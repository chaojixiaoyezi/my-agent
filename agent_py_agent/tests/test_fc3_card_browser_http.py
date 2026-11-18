# 模块用途: FC3（C7 受控两阶段 browser-lite 凭据填表 + C8 声明式 HTTP collected/form/415/MFA + C5 脱敏委派）端到端与组件验收测试。
"""FC3 真实插件安装包、PluginMCPClient/PluginProxyTool、无头 Chromium 与真实回环 HTTP 服务验收测试。"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import threading
import time
import zipfile
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from urllib.parse import parse_qs, quote, quote_plus

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.conversation.tool_approval_scope import (
    tool_approval_scope,
    tool_approval_scope_is_current,
)
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_management import PluginManagement, PluginManagementContext
from agent_py_agent.agent.plugin_observation import OBSERVATION_KEY
from agent_py_agent.agent.plugin_runtime import PluginMCPClient, plugin_tool_name
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.tooling import host_output_redaction
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.web_fetch_tools import _redact_secret_echo, _secret_echo_variants
from agent_py_agent.agent.user_space import task_credentials
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.agent.user_space.task_credential_http import credential_authenticated_request
from agent_py_agent.agent.user_space.task_credential_schema import (
    credential_auth_config,
    credential_fields,
    credential_ready_expired,
)
from agent_py_agent.agent.user_space.task_credential_storage import CredentialInputError
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    _cipher,
    _credential_for_use,
    _credential_mutation,
    _save_credential_row,
    credential_path,
    manage_credential,
    request_credential_card,
)
from agent_py_agent.tests.plugin_enable_fixtures import enable_with_confirmation
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_browser_lite_package import HAS_BROWSER, gone, needs_browser
from agent_py_agent.tests.test_mcp_client import _config as _mcp_config
from agent_py_agent.tests.test_web_fetch_ssrf import _tool as _web_fetch_tool
from agent_py_agent.tests.test_web_fetch_ssrf import server as _ssrf_server_fixture
from agent_py_agent.tests.test_workspace_read_context import read_context
from scripts.build_plugin_api import ROOT, build_plugin_api
from scripts.build_plugin_package import build_plugin_package

_FIXTURES = (background,)
server = _ssrf_server_fixture

PROTECTED_RECORD_MARKER = "RECORD-MARKER-FC3-9941"
SYNTHETIC_USER = "ops user α"
SYNTHETIC_SECRET = "Canary Secret 77!@# β"
SYNTHETIC_BEARER_TOKEN = "Canary-Bearer-Token-77+Plus/End"


@pytest.fixture(scope="module")
def browser_lite_bundle(tmp_path_factory) -> Path:
    build_root = tmp_path_factory.mktemp("fc3-browser-lite-bundle")
    sdk_wheel = build_plugin_api(build_root / "sdk")
    return build_plugin_package(
        ROOT / "plugins/browser-lite",
        "browser_lite/declaration.json",
        (sdk_wheel,),
        build_root / "browser-lite.zip",
    )


def _compute_js_login_digest(username: str, secret: str) -> str:
    joined = f"fc3-v1|{username}|{secret}".encode()
    shifted = bytes(((byte ^ 0x5A) & 0xFF) for byte in joined)
    return base64.b64encode(shifted).decode("ascii")


def _all_secret_variants(values: tuple[str, ...]) -> tuple[str, ...]:
    expanded: set[str] = set()
    for raw in values:
        if not raw:
            continue
        expanded.update((
            raw,
            json.dumps(raw, ensure_ascii=True)[1:-1],
            json.dumps(raw, ensure_ascii=False)[1:-1],
            quote(raw, safe=""),
            quote_plus(raw, safe=""),
        ))
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encoder(raw.encode("utf-8")).decode("ascii")
            expanded.update((encoded, encoded.rstrip("=")))
    return tuple(sorted((item for item in expanded if item), key=len, reverse=True))


@dataclass(frozen=True)
class _FallbackMaterial:
    credential_ref: str
    generation: int
    origin: str
    target_url: str
    values: MappingProxyType
    requested_credential_ref: str = ""
    access_scope: str = "task"
    owner_id: str = ""
    actor_id: str = ""
    thread_id: str = ""
    browser_consumer: MappingProxyType | None = None


class _TurnRedactionRecorder:
    """兼容 FC2 TurnCredentialRedaction 接口的测试净化器；集成 FC2 后同步校验生产实现。"""

    def __init__(self, agent):
        self._values: set[str] = set()
        factory = getattr(host_output_redaction, "turn_credential_redaction", None)
        self._delegate = factory(agent, getattr(agent, "_current_run_params", None)) if callable(factory) else None

    def register(self, material) -> None:
        self.register_values(material.values.values())
        if self._delegate is not None and hasattr(self._delegate, "register"):
            self._delegate.register(material)

    def register_values(self, values) -> None:
        for item in values:
            if isinstance(item, str) and item:
                self._values.add(item)
        if self._delegate is not None and hasattr(self._delegate, "register_values"):
            self._delegate.register_values(values)

    def public_text(self, text: str) -> str:
        out = str(text)
        if self._delegate is not None and hasattr(self._delegate, "public_text"):
            out = self._delegate.public_text(out)
        for variant in _all_secret_variants(tuple(self._values)):
            out = out.replace(variant, "[REDACTED]")
        return out


class _HostCredentialUseAdapter:
    """当 FC1/FC2 已合入时直接委派生产 authorized_credential_material；单分片独立运行时按 C1/C5 合同精确复核。"""

    def __init__(
        self,
        agent,
        *,
        owner_id: str,
        actor_id: str,
        thread_id: str,
        active: bool = True,
        after_inspect_hook=None,
    ):
        self._agent = agent
        self._owner_id = owner_id
        self._actor_id = actor_id
        self._thread_id = thread_id
        self._active = active
        self._after_inspect_hook = after_inspect_hook
        self.turn_redaction = _TurnRedactionRecorder(agent)
        self.calls: list[dict[str, object]] = []
        self.decrypt_count = 0

    def session_scope(self) -> dict[str, object]:
        current = getattr(self._agent, "_current_run_params", None)
        run_id = str(getattr(current, "run_id", "") or "")
        return {
            "active": self._active,
            "owner_id": self._owner_id,
            "owner_home": str(self._agent.home_paths.owner_home_dir),
            "channel": "feishu",
            "actor_id": self._actor_id,
            "thread_id": self._thread_id,
            "root_task_id": run_id,
            "run_id": run_id,
            "attempt_id": "attempt-fc3",
        }

    def register_values(self, values) -> None:
        self.turn_redaction.register_values(values)

    @contextlib.contextmanager
    def __call__(
        self,
        reference: str,
        *,
        consumer: str,
        target_url: str | None = None,
        expected_generation: int | None = None,
        **extra,
    ):
        self.calls.append({
            "reference": reference,
            "consumer": consumer,
            "target_url": target_url,
            "expected_generation": expected_generation,
        })
        if callable(self._after_inspect_hook):
            self._after_inspect_hook()
        prod_cm = getattr(task_credentials, "authorized_credential_material", None)
        if callable(prod_cm) and self._actor_id == "ou_synthetic" and self._active:
            with prod_cm(
                self._agent,
                reference,
                consumer=consumer,
                target_url=target_url,
                expected_generation=expected_generation,
                browser_consumer=extra.get("browser_consumer"),
            ) as material:
                self.decrypt_count += 1
                self.turn_redaction.register(material)
                yield material
            return
        if not self._active:
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "当前任务执行作用域已失效",
                stage="scope_check",
                effect_outcome="not_started",
            )
        current = self._agent._current_run_params
        scope = tool_approval_scope(self._agent, str(current.run_id or current.task_id))
        if not scope.active or not tool_approval_scope_is_current(self._agent, scope):
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "原任务执行已失效",
                stage="scope_check",
                effect_outcome="not_started",
            )
        if self._actor_id != "ou_synthetic" or self._thread_id != scope.thread_id:
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "凭据仅限原发起人在原会话使用",
                stage="actor_thread_check",
                effect_outcome="not_started",
            )
        try:
            row = _credential_for_use(self._agent, reference, scope, target_url or "")
        except (PermissionError, ValueError) as exc:
            code = "CREDENTIAL_NOT_AUTHORIZED" if isinstance(exc, PermissionError) else "CREDENTIAL_INPUT_UNAVAILABLE"
            raise CredentialInputError(code, str(exc), stage="credential_select", effect_outcome="not_started") from exc
        if expected_generation is not None and int(row.get("generation", 0)) != expected_generation:
            raise CredentialInputError(
                "CREDENTIAL_INPUT_UNAVAILABLE",
                "凭据代次已变化",
                stage="generation_check",
                effect_outcome="not_started",
            )
        if credential_ready_expired(row, now=time.time()):
            raise CredentialInputError(
                "CREDENTIAL_INPUT_UNAVAILABLE",
                "凭据已过期",
                stage="expiry_check",
                effect_outcome="not_started",
            )
        expected_consumer = row.get("browser_consumer")
        actual_consumer = extra.get("browser_consumer")
        if isinstance(expected_consumer, dict) and dict(expected_consumer) != dict(actual_consumer or {}):
            raise CredentialInputError(
                "CREDENTIAL_NOT_AUTHORIZED",
                "凭据绑定的浏览器消费者不匹配",
                stage="browser_consumer_check",
                effect_outcome="not_started",
            )
        cipher = _cipher(self._agent.home_paths.owner_home_dir)
        decrypted = json.loads(cipher.decrypt(row["ciphertext"].encode("utf-8")))
        self.decrypt_count += 1
        material = _FallbackMaterial(
            credential_ref=str(row["credential_ref"]),
            generation=int(row["generation"]),
            origin=str(row["origin"]),
            target_url=str(row.get("target_url") or target_url or ""),
            values=MappingProxyType(dict(decrypted)),
            requested_credential_ref=reference,
            access_scope=str(row.get("access_scope") or "task"),
            owner_id=self._owner_id,
            actor_id=self._actor_id,
            thread_id=self._thread_id,
            browser_consumer=MappingProxyType(dict(expected_consumer)) if isinstance(expected_consumer, dict) else None,
        )
        self.turn_redaction.register(material)
        yield material


def _invocation_context(
    snapshot,
    workspace: Path,
    *,
    credential_use: object | None,
) -> ToolInvocationContext:
    read_ctx = read_context(workspace)
    kwargs: dict[str, object] = {
        "runtime_snapshot": snapshot,
        "workspace_read_context": read_ctx,
    }
    if "credential_use" in ToolInvocationContext.__dataclass_fields__:
        kwargs["credential_use"] = credential_use
        return ToolInvocationContext(**kwargs)
    ctx = ToolInvocationContext(**kwargs)
    if credential_use is not None:
        object.__setattr__(ctx, "credential_use", credential_use)
    return ctx


def _setup_im_card(
    ctx,
    url: str,
    *,
    authentication: str = "collected",
    collection_mode: str = "username_password",
    fields: list[dict[str, str]] | None = None,
    auth_config: dict | None = None,
    header_name: str = "Authorization",
    submit_values: dict[str, str] | None = None,
    ttl_seconds: int = 600,
) -> tuple[str, Path, dict[str, str]]:
    ctx.agent.config.feishu_session_lock_enabled = False
    ctx.agent._current_run_params = SimpleNamespace(run_id=ctx.task_id, task_id=ctx.task_id, task_attributes={})
    bind_admin_channel_identity(ctx.agent.home_paths.root, "feishu", "ou_synthetic")
    sent_cards: list[dict] = []

    def send_card(_target, card, **_kwargs):
        sent_cards.append(card)
        return {"message_id": "om_" + os.urandom(8).hex(), "chat_id": "oc_private_fc3"}

    ctx.agent.channel_registry.register_adapter(
        "feishu",
        SimpleNamespace(send_control_card=send_card),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True),
    )
    options = CredentialCardOptions(
        fields=fields,
        auth_config=auth_config,
        ttl_seconds=ttl_seconds,
        collection_mode=collection_mode,
    )
    reference, _ = request_credential_card(
        ctx.agent, url=url, authentication=authentication, header_name=header_name, options=options,
    )
    path = credential_path(ctx.agent.home_paths.owner_home_dir, reference)
    row = read_json_object_report(path, context="fc3.test").payload
    if submit_values is None:
        if authentication in {"bearer", "header"}:
            submit_values = {"secret": SYNTHETIC_BEARER_TOKEN}
        elif authentication == "collected" and collection_mode == "password":
            submit_values = {"secret": SYNTHETIC_SECRET}
        else:
            submit_values = {"username": SYNTHETIC_USER, "secret": SYNTHETIC_SECRET}
    submission = {
        "reference": row["reference"],
        "decision": "submit",
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "fields": dict(submit_values),
    }
    outcome = resolve_private_interaction(ctx.agent, submission, channel="feishu", actor_id="ou_synthetic")
    assert outcome["ok"], outcome
    return reference, path, dict(submit_values)


def _install_and_enable_browser_lite(ctx, tmp_path: Path, bundle_path: Path, *, allowed_hosts=("127.0.0.1",)):
    owner = resolve_owner_home(ctx.agent.home_paths.root)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    mgmt_ctx = PluginManagementContext(
        owner,
        "ou_synthetic",
        "feishu",
        "session-fc3",
        ctx.agent.conversation_store.threads,
        workspace,
        PathAccessPolicy.from_values(mode="full"),
        True,
        True,
        legacy_sandbox_default=False,
    )
    service = PluginManagement(mgmt_ctx)
    installed = service.command(
        f'/plugins install "{bundle_path}"',
        revision=service.catalog().revision,
        request_id="install-browser-lite",
    )
    assert installed["state"] == "succeeded", installed
    settings_file = tmp_path / "browser-settings.json"
    settings_file.write_text(
        json.dumps({"allowed_hosts": list(allowed_hosts), "command_timeout_seconds": 45}),
        encoding="utf-8",
    )
    configured = service.command(
        f'/plugins configure browser-lite --file "{settings_file}"',
        revision=service.catalog().revision,
        request_id="configure-browser-lite",
    )
    assert configured["state"] == "succeeded", configured
    enable_with_confirmation(service, "browser-lite", request_id="enable-browser-lite")
    repo = RuntimeRepository(runtime_db_path(owner.home_dir))
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace,
            max_chars=10000,
            max_entries=100,
            max_matches=100,
            web_max_chars=10000,
            http_timeout=5,
            catalog_limit=100,
            retrieval_limit=10,
            vector_search_enabled=False,
            plugin_owner=owner,
            runtime_repo=repo,
        )
    ).with_access_policy(access_mode="full-access", path_access_mode="full", owner_scope_root="")
    registry.prepare_for_run()
    client = next(item for item in registry._mcp_clients if isinstance(item, PluginMCPClient))
    return service, owner, workspace, registry, client, repo


class _LoopbackProtectedSite:
    """绑定 127.0.0.1 的受保护测试站点：/signin 前端 JS 变换登录、/login 拒原生 JSON(415)、/account 校验 Cookie。"""

    def __init__(self, *, cross_origin_port: int | None = None):
        self.expected_digest = _compute_js_login_digest(SYNTHETIC_USER, SYNTHETIC_SECRET)
        self.session_token = "SessionCookieToken-FC3-8821"
        self.cross_origin_port = cross_origin_port
        self.audit_events: list[dict[str, object]] = []
        self._lock = threading.Lock()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _fmt, *_args):
                return

            def _record(self, status: int, raw_body: bytes = b"", *, valid_digest: bool = False):
                raw_hit = any(
                    token.encode("utf-8") in raw_body
                    for token in (SYNTHETIC_SECRET,)
                )
                with site._lock:
                    site.audit_events.append({
                        "method": self.command,
                        "path": self.path,
                        "status": status,
                        "raw_secret_on_wire": raw_hit,
                        "valid_js_digest": valid_digest,
                    })

            def _send(self, status: int, content_type: str, body: bytes, extra_headers: dict[str, str] | None = None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, val in (extra_headers or {}).items():
                    self.send_header(key, val)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/signin":
                    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>企业门户登录</title></head>
<body>
  <form id="login-form" action="/login" method="post">
    <input id="username" name="username" type="text" value="">
    <input id="password" name="password" type="password" value="">
    <input id="note" name="note" type="text" value="">
    <button id="submit" type="submit">登录</button>
  </form>
  <p id="status">等待登录</p>
  <script src="/transform.js"></script>
</body></html>""".encode()
                    self._record(200)
                    self._send(200, "text/html; charset=utf-8", html)
                    return
                if self.path == "/transform.js":
                    js = """
document.getElementById("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const user = document.getElementById("username").value;
  const sec = document.getElementById("password").value;
  const note = document.getElementById("note").value;
  const raw = new TextEncoder().encode("fc3-v1|" + user + "|" + sec);
  const xored = Array.from(raw, (b) => String.fromCharCode((b ^ 0x5a) & 0xff)).join("");
  const digest = btoa(xored);
  const body = new URLSearchParams({user, digest, note}).toString();
  const resp = await fetch("/login", {
    method: "POST",
    headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "X-Client-Transform": "fc3-v1"},
    body,
    credentials: "same-origin"
  });
  if (resp.ok) {
    window.location.href = "/account";
  } else {
    document.getElementById("status").textContent = "登录失败:" + resp.status;
  }
});
""".encode()
                    self._record(200)
                    self._send(200, "application/javascript; charset=utf-8", js)
                    return
                if self.path == "/account":
                    cookie = SimpleCookie()
                    cookie.load(self.headers.get("Cookie", ""))
                    if "fc3_sid" not in cookie or cookie["fc3_sid"].value != site.session_token:
                        self._record(401)
                        self._send(401, "text/html; charset=utf-8", b"<html><body>unauthorized</body></html>")
                        return
                    html = (
                        f"<html><head><title>账户详情</title></head>"
                        f"<body><div id='record'>{PROTECTED_RECORD_MARKER}</div>"
                        f"<button id='refresh' type='button'>刷新</button></body></html>"
                    ).encode()
                    self._record(200)
                    self._send(200, "text/html; charset=utf-8", html)
                    return
                if self.path == "/echo-variants":
                    b64_no_pad = base64.b64encode(SYNTHETIC_SECRET.encode()).decode("ascii").rstrip("=")
                    url_plus = quote_plus(SYNTHETIC_SECRET, safe="")
                    html = (
                        "<html><head><title>回显检查</title></head><body>"
                        f"<input id='echo-box' type='text' value='{url_plus}'>"
                        f"<button id='echo-btn' type='button'>动作:{b64_no_pad}</button>"
                        "</body></html>"
                    ).encode()
                    self._record(200)
                    self._send(200, "text/html; charset=utf-8", html)
                    return
                if self.path == "/cross-form":
                    other_port = site.cross_origin_port or 9
                    html = (
                        "<html><head><title>外部表单页</title></head><body>"
                        f"<form id='ext' action='http://127.0.0.1:{other_port}/collect' method='post'>"
                        "<input id='password' name='password' type='password' value=''>"
                        "</form></body></html>"
                    ).encode()
                    self._record(200)
                    self._send(200, "text/html; charset=utf-8", html)
                    return
                self._record(404)
                self._send(404, "text/plain; charset=utf-8", b"not found")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length > 0 else b""
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if self.path == "/login":
                    if ctype != "application/x-www-form-urlencoded" or self.headers.get("X-Client-Transform") != "fc3-v1":
                        self._record(415, raw_body)
                        self._send(415, "application/json", b'{"error":"client_js_transform_required"}')
                        return
                    parsed = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
                    user = (parsed.get("user") or [""])[0]
                    digest = (parsed.get("digest") or [""])[0]
                    valid = user == SYNTHETIC_USER and digest == site.expected_digest
                    if not valid:
                        self._record(401, raw_body, valid_digest=False)
                        self._send(401, "application/json", b'{"error":"invalid_credentials"}')
                        return
                    self._record(200, raw_body, valid_digest=True)
                    self._send(
                        200,
                        "application/json",
                        b'{"ok":true}',
                        {"Set-Cookie": f"fc3_sid={site.session_token}; HttpOnly; Path=/"},
                    )
                    return
                self._record(404, raw_body)
                self._send(404, "text/plain; charset=utf-8", b"not found")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def dual_sites():
    secondary = _LoopbackProtectedSite()
    primary = _LoopbackProtectedSite(cross_origin_port=secondary.port)
    try:
        yield primary, secondary
    finally:
        primary.close()
        secondary.close()


def _record_observation_event(agent, repo: RuntimeRepository, outcome, *, run_scope: dict[str, str], op_id: str) -> list[dict]:
    if repo.agent_run_for_run_id(run_scope["run_id"]) is None:
        now = time.time()
        task_run_id = f"taskrun-{run_scope['task_id']}"
        agent_run_id = f"agentrun-{run_scope['run_id']}"
        attempt_id = run_scope["attempt_id"]
        with repo._runtime_connection() as conn:
            conn.execute(
                "INSERT INTO task_runs(task_run_id, task_id, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                (task_run_id, run_scope["task_id"], "running", now, now),
            )
            conn.execute(
                "INSERT INTO agent_runs(agent_run_id, task_run_id, run_id, role, status, current_attempt_id, "
                "current_attempt_generation, workspace_epoch, created_at, updated_at) VALUES(?,?,?,?,?,?,1,1,?,?)",
                (agent_run_id, task_run_id, run_scope["run_id"], "main", "running", attempt_id, now, now),
            )
            conn.execute(
                "INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation, status, started_at, metadata_json) "
                "VALUES(?,?,1,?,?,?)",
                (attempt_id, agent_run_id, "running", now, "{}"),
            )
            conn.commit()
    archive = {
        "run_id": run_scope["run_id"],
        "task_id": run_scope["task_id"],
        "operation_id": op_id,
        "attempt_id": run_scope["attempt_id"],
        "tool": outcome.tool,
        "ok": outcome.ok,
        "error_code": outcome.error_code,
        "idempotency_key": "",
        "tool_result_envelope": outcome.result_envelope,
    }
    persist_tool_runtime_ledger(
        SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo), local_store=agent.local_store),
        archive,
    )
    visible = json.loads(outcome.output).get("structuredContent", {}).get(OBSERVATION_KEY, {})
    return list(visible.get("candidates") or [])


def _assert_zero_secret_variants(text: str, values: tuple[str, ...]) -> None:
    for variant in _all_secret_variants(values):
        assert variant not in text


@needs_browser
def test_real_package_browser_credential_fill_js_login_session_isolation_and_cleanup(
    background,
    tmp_path,
    browser_lite_bundle,
    dual_sites,
):
    primary_site, _secondary_site = dual_sites
    _service, owner, workspace, registry, client, repo = _install_and_enable_browser_lite(
        background, tmp_path, browser_lite_bundle,
    )
    try:
        ref, _card_path, submitted = _setup_im_card(
            background,
            primary_site.origin + "/signin",
            authentication="collected",
        )
        secret_tuple = (submitted["username"], submitted["secret"])
        scope = tool_approval_scope(background.agent, background.task_id)
        cred_use = _HostCredentialUseAdapter(
            background.agent,
            owner_id=owner.owner_id,
            actor_id="ou_synthetic",
            thread_id=scope.thread_id,
        )
        snapshot = registry.runtime_snapshot(run_id=background.task_id)
        ctx = _invocation_context(snapshot, workspace, credential_use=cred_use)
        run_scope = {
            "owner_id": owner.owner_id,
            "owner_home": str(owner.home_dir),
            "run_id": background.task_id,
            "task_id": background.task_id,
            "attempt_id": "attempt-1",
            "turn_id": "turn-1",
        }

        open_tool = registry.tools[plugin_tool_name("browser-lite", "open")]
        read_tool = registry.tools[plugin_tool_name("browser-lite", "read")]
        fill_tool = registry.tools[plugin_tool_name("browser-lite", "fill")]
        click_tool = registry.tools[plugin_tool_name("browser-lite", "click")]
        close_tool = registry.tools[plugin_tool_name("browser-lite", "close")]

        # 1) 匿名直接访问 /account 应返回 401 页面，不泄露受保护记录
        anon_check = _web_fetch_tool()
        anon_res = anon_check.execute({"url": primary_site.origin + "/account", "mode": "text"})
        assert not anon_res.ok and anon_res.result_envelope.get("http_status") == 401
        assert PROTECTED_RECORD_MARKER not in anon_res.output

        # 2) collected 卡在 HTTP 预检零发包、零解密，返回原引用与 browser-lite / configure / run_command 调法
        events_before = len(primary_site.audit_events)
        http_tool = _web_fetch_tool()
        http_tool.credential_authenticator = lambda r, req, send: credential_authenticated_request(
            background.agent, r, req, send=send, credential_use=cred_use,
        )
        preflight = http_tool.execute_scoped(
            {"url": primary_site.origin + "/account", "credential_ref": ref, "mode": "text"},
            ctx,
        )
        assert not preflight.ok and preflight.error_code == "CREDENTIAL_BINDING_INVALID"
        assert preflight.effect_outcome == "not_started"
        assert preflight.result_envelope["credential_ref"] == ref
        assert preflight.result_envelope["outbound_requests"] == 0
        assert len(primary_site.audit_events) == events_before
        assert cred_use.decrypt_count == 0

        # 3) 打开 /signin，验证原普通 value="" 与非空字面值 fill 仍正常工作
        opened = open_tool.execute_scoped({"url": primary_site.origin + "/signin", "__run_scope": run_scope}, ctx)
        assert opened.ok, opened.output
        opened_body = json.loads(json.loads(opened.output)["result"])
        actor_a_pid = int(opened_body["browser_pid"])

        empty_literal = fill_tool.execute_scoped(
            {"selector": "#note", "value": "", "__run_scope": run_scope},
            ctx,
        )
        assert empty_literal.ok
        assert json.loads(json.loads(empty_literal.output)["result"]) == {"filled": "input", "value": ""}
        note_literal = fill_tool.execute_scoped(
            {"selector": "#note", "value": "desk-check", "__run_scope": run_scope},
            ctx,
        )
        assert note_literal.ok
        assert json.loads(json.loads(note_literal.output)["result"]) == {"filled": "input", "value": "desk-check"}

        # 4) C7 两阶段凭据填表（selector 路径填 username + 观察 candidate_id 路径填 secret）
        user_fill = fill_tool.execute_scoped(
            {"selector": "#username", "credential_ref": ref, "field": "username", "__run_scope": run_scope},
            ctx,
        )
        assert user_fill.ok, user_fill.output
        user_fill_body = json.loads(json.loads(user_fill.output)["result"])
        assert user_fill_body == {"filled": "input", "credential_applied": True}
        _assert_zero_secret_variants(user_fill.output, secret_tuple)

        read_signin = read_tool.execute_scoped(
            {"__run_scope": run_scope, "__operation_id": "op-read-signin"},
            ctx,
        )
        assert read_signin.ok, read_signin.output
        _assert_zero_secret_variants(read_signin.output, secret_tuple)
        _assert_zero_secret_variants(json.dumps(read_signin.result_envelope, ensure_ascii=False), secret_tuple)
        signin_items = {
            item["id"]: item
            for item in json.loads(json.loads(read_signin.output)["result"])["items"]
        }
        # 已填凭据控件与 password 控件必须隐藏 value，普通控件 #note 保留可见 value
        assert "value" not in signin_items["username"] and signin_items["username"]["text"] == ""
        assert "value" not in signin_items["password"] and signin_items["password"]["text"] == ""
        assert signin_items["note"]["value"] == "desk-check"

        candidates = _record_observation_event(
            background.agent, repo, read_signin, run_scope=run_scope, op_id="op-read-signin",
        )
        password_cand = next(item for item in candidates if "password" in item["label"])
        secret_fill = fill_tool.execute_scoped(
            {
                "candidate_id": password_cand["candidate_id"],
                "credential_ref": ref,
                "field": "secret",
                "__run_scope": run_scope,
            },
            ctx,
        )
        assert secret_fill.ok, secret_fill.output
        secret_fill_body = json.loads(json.loads(secret_fill.output)["result"])
        assert secret_fill_body == {"filled": "input", "credential_applied": True}
        assert secret_fill.result_envelope["observation_action"]["candidate_id"] == password_cand["candidate_id"]
        _assert_zero_secret_variants(secret_fill.output, secret_tuple)

        # 5) 点击提交，网页自身 /transform.js 在浏览器内完成变换登录并跳转到 /account
        clicked = click_tool.execute_scoped({"selector": "#submit", "__run_scope": run_scope}, ctx)
        assert clicked.ok, clicked.output
        clicked_body = json.loads(json.loads(clicked.output)["result"])
        assert clicked_body["url"].endswith("/account")
        account_read = read_tool.execute_scoped(
            {"selector": "#record", "__run_scope": run_scope, "__operation_id": "op-read-account"},
            ctx,
        )
        assert account_read.ok, account_read.output
        assert PROTECTED_RECORD_MARKER in account_read.output
        login_events = [ev for ev in primary_site.audit_events if ev["path"] == "/login"]
        assert len(login_events) == 1
        assert login_events[0]["status"] == 200
        assert login_events[0]["valid_js_digest"] is True
        assert login_events[0]["raw_secret_on_wire"] is False

        # 6) 同 (owner, actor, thread) 会话复用 + 页面/候选/回合流式与出站变体净化验证
        reused = open_tool.execute_scoped({"url": primary_site.origin + "/echo-variants", "__run_scope": run_scope}, ctx)
        assert reused.ok, reused.output
        reused_body = json.loads(json.loads(reused.output)["result"])
        assert int(reused_body["browser_pid"]) == actor_a_pid
        echo_read = read_tool.execute_scoped({"__run_scope": run_scope, "__operation_id": "op-read-echo"}, ctx)
        assert echo_read.ok, echo_read.output
        assert "[REDACTED]" in echo_read.output
        _assert_zero_secret_variants(echo_read.output, secret_tuple)
        _assert_zero_secret_variants(json.dumps(echo_read.result_envelope, ensure_ascii=False), secret_tuple)
        # SSE / 出站投递公开文本同样经回合脱敏器净化全部编码变体
        b64_secret = base64.b64encode(submitted["secret"].encode("utf-8")).decode("ascii")
        outbound_sample = f"结果摘要 {PROTECTED_RECORD_MARKER} 附带回显 {b64_secret}"
        scrubbed_outbound = cred_use.turn_redaction.public_text(outbound_sample)
        assert PROTECTED_RECORD_MARKER in scrubbed_outbound and "[REDACTED]" in scrubbed_outbound
        _assert_zero_secret_variants(scrubbed_outbound, secret_tuple)

        # 7) 跨 actor、跨 thread 与无绑定接管一律拒绝（CREDENTIAL_NOT_AUTHORIZED / not_started）
        unbound_ctx = _invocation_context(snapshot, workspace, credential_use=None)
        for denied_ctx in (
            unbound_ctx,
            _invocation_context(
                snapshot,
                workspace,
                credential_use=_HostCredentialUseAdapter(
                    background.agent, owner_id=owner.owner_id, actor_id="ou_other_member", thread_id=scope.thread_id,
                ),
            ),
            _invocation_context(
                snapshot,
                workspace,
                credential_use=_HostCredentialUseAdapter(
                    background.agent, owner_id=owner.owner_id, actor_id="ou_synthetic", thread_id="thread-other",
                ),
            ),
        ):
            denied_read = read_tool.execute_scoped({"__run_scope": run_scope, "__operation_id": "op-denied"}, denied_ctx)
            assert not denied_read.ok and denied_read.error_code == "CREDENTIAL_NOT_AUTHORIZED"
            assert denied_read.effect_outcome == "not_started"
            denied_click = click_tool.execute_scoped({"selector": "#echo-btn", "__run_scope": run_scope}, denied_ctx)
            assert not denied_click.ok and denied_click.error_code == "CREDENTIAL_NOT_AUTHORIZED"
            assert denied_click.effect_outcome == "not_started"

        # 另一位成员在本人会话独立 open 时拿到独立隔离浏览器进程且不共享已登录 Cookie（/account 仍 401）
        actor_b_ctx = _invocation_context(
            snapshot,
            workspace,
            credential_use=_HostCredentialUseAdapter(
                background.agent, owner_id=owner.owner_id, actor_id="ou_other_member", thread_id="thread-member-b",
            ),
        )
        actor_b_open = open_tool.execute_scoped({"url": primary_site.origin + "/account", "__run_scope": run_scope}, actor_b_ctx)
        assert actor_b_open.ok, actor_b_open.output
        actor_b_body = json.loads(json.loads(actor_b_open.output)["result"])
        actor_b_pid = int(actor_b_body["browser_pid"])
        assert actor_b_pid != actor_a_pid
        assert PROTECTED_RECORD_MARKER not in actor_b_body["text"]
        assert close_tool.execute_scoped({"__run_scope": run_scope}, actor_b_ctx).ok
        assert gone(actor_b_pid)

        # 8) 原会话 close 后隔离进程退出、临时 sessions/cred-* 目录彻底删除，插件目录零秘密残留
        closed_a = close_tool.execute_scoped({"__run_scope": run_scope}, ctx)
        assert closed_a.ok
        assert gone(actor_a_pid)
        sessions_dir = owner.plugins_dir / "data" / "browser-lite" / "sessions"
        assert not sessions_dir.exists() or not list(sessions_dir.iterdir())
        for file_path in owner.plugins_dir.rglob("*"):
            if file_path.is_file():
                raw_bytes = file_path.read_bytes()
                assert submitted["secret"].encode("utf-8") not in raw_bytes
    finally:
        registry.close_mcp_clients()


def test_browser_fill_parameter_exclusivity_missing_field_and_consumer_verification(
    background,
    tmp_path,
    browser_lite_bundle,
    dual_sites,
):
    primary_site, _secondary_site = dual_sites
    service, owner, workspace, registry, client, _repo = _install_and_enable_browser_lite(
        background, tmp_path, browser_lite_bundle,
    )
    try:
        ref, card_path, _submitted = _setup_im_card(
            background, primary_site.origin + "/signin", authentication="collected",
        )
        scope = tool_approval_scope(background.agent, background.task_id)
        cred_use = _HostCredentialUseAdapter(
            background.agent, owner_id=owner.owner_id, actor_id="ou_synthetic", thread_id=scope.thread_id,
        )
        snapshot = registry.runtime_snapshot(run_id=background.task_id)
        ctx = _invocation_context(snapshot, workspace, credential_use=cred_use)
        fill_tool = registry.tools[plugin_tool_name("browser-lite", "fill")]

        # 1) 参数互斥与缺失校验均在解密前返回 TOOL_INVALID_ARGUMENTS / not_started
        bad_cases = [
            {"selector": "#password", "value": "literal", "credential_ref": ref, "field": "secret"},
            {"selector": "#password", "value": "literal", "field": "secret"},
            {"selector": "#password", "credential_ref": ref},
            {"selector": "#password", "field": "secret"},
            {"selector": "#password"},
            {"selector": "#password", "credential_ref": "cred_invalid", "field": "secret"},
            {"credential_ref": ref, "field": "secret"},
        ]
        for bad_args in bad_cases:
            outcome = fill_tool.execute_scoped(bad_args, ctx)
            assert not outcome.ok and outcome.error_code == "TOOL_INVALID_ARGUMENTS", bad_args
            assert outcome.effect_outcome == "not_started"
        assert cred_use.decrypt_count == 0

        # 2) 直接通过底层 MCP 无 _meta.credential_fill 调用插件，插件必须拒绝凭据填表（MISSING_CONTEXT）
        direct_env = dict(client.config.env)
        direct_client = MCPStdioClient(
            _mcp_config("", name="browser-lite", command=client.config.command, args=list(client.config.args), env=direct_env, timeout=30.0)
        )
        direct_client.start()
        try:
            raw_res = direct_client.call_tool(
                "fill",
                {"selector": "#password", "credential_ref": ref, "field": "secret"},
                request_meta={"my-agent/workspace-read": read_context(workspace).to_payload()},
            )
            assert raw_res["isError"] is True
            err_body = json.loads(raw_res["content"])
            assert err_body["code"] == "MISSING_CONTEXT"
            assert err_body["side_effect"] == "not_applied"
        finally:
            direct_client.stop()

        # 3) 若浏览器激活重装/换代或原卡绑定消费者代次不一致，拒绝跨代继承授权（0 apply）
        if HAS_BROWSER:
            open_tool = registry.tools[plugin_tool_name("browser-lite", "open")]
            opened = open_tool.execute_scoped({"url": primary_site.origin + "/signin"}, ctx)
            assert opened.ok
            # 传错误的字段名 'password'（内置卡键为 'secret'），明确提示改用 'secret' 且不填入页面
            wrong_field = fill_tool.execute_scoped(
                {"selector": "#password", "credential_ref": ref, "field": "password"},
                ctx,
            )
            assert not wrong_field.ok and wrong_field.error_code == "TOOL_INVALID_ARGUMENTS"
            assert wrong_field.effect_outcome == "not_started"
            assert "secret" in wrong_field.output and "secret" in wrong_field.next_action

            # 验证插件重新启用换代或原卡绑定旧激活代次时，拒绝跨代继承授权
            existing_row = read_json_object_report(card_path, context="fc3.consumer").payload
            if "browser_consumer" in existing_row:
                disabled = service.command(
                    "/plugins disable browser-lite",
                    revision=service.catalog().revision,
                    request_id="disable-browser-lite",
                )
                assert disabled["state"] == "succeeded", disabled
                enable_with_confirmation(service, "browser-lite", request_id="reenable-browser-lite")
                registry.prepare_for_run()
                re_snapshot = registry.runtime_snapshot(run_id=background.task_id)
                re_ctx = _invocation_context(re_snapshot, workspace, credential_use=cred_use)
                re_open = registry.tools[plugin_tool_name("browser-lite", "open")]
                re_fill = registry.tools[plugin_tool_name("browser-lite", "fill")]
                assert re_open.execute_scoped({"url": primary_site.origin + "/signin"}, re_ctx).ok
                stale_consumer = re_fill.execute_scoped(
                    {"selector": "#password", "credential_ref": ref, "field": "secret"},
                    re_ctx,
                )
            else:
                with _credential_mutation(background.agent, card_path) as admission:
                    row = read_json_object_report(card_path, context="fc3.consumer").payload
                    row["browser_consumer"] = {
                        "installation_ref": client.installation.installation_ref,
                        "package_sha256": client.installation.package_sha256,
                        "activation_id": "0" * 64,
                        "tool": "fill",
                    }
                    _save_credential_row(admission, card_path, row)
                stale_consumer = fill_tool.execute_scoped(
                    {"selector": "#password", "credential_ref": ref, "field": "secret"},
                    ctx,
                )
            assert not stale_consumer.ok and stale_consumer.error_code == "CREDENTIAL_NOT_AUTHORIZED"
            assert stale_consumer.effect_outcome == "not_started"

        # 4) 篡改非内置 wheel 包字节时，_verify_browser_lite_consumer 在解密前拦截并指引同卡改走脚本/HTTP
        buf = io.BytesIO()
        with zipfile.ZipFile(browser_lite_bundle, "r") as src_zip, zipfile.ZipFile(buf, "w") as dst_zip:
            for item in src_zip.infolist():
                data = src_zip.read(item.filename)
                if item.filename.startswith("wheels/my_agent_browser_lite-"):
                    wbuf = io.BytesIO()
                    with zipfile.ZipFile(io.BytesIO(data), "r") as wsrc, zipfile.ZipFile(wbuf, "w") as wdst:
                        for witem in wsrc.infolist():
                            wdata = wsrc.read(witem.filename)
                            if witem.filename == "browser_lite/server.py":
                                wdata = wdata + b"\n# non-builtin wheel marker\n"
                            wdst.writestr(witem, wdata)
                    data = wbuf.getvalue()
                dst_zip.writestr(item, data)
        assert not _verify_wheel_helper(buf.getvalue(), client.installation.manifest.entry_wheel)
        _ = service
    finally:
        registry.close_mcp_clients()


def _verify_wheel_helper(archive_bytes: bytes, entry_wheel: str) -> bool:
    from agent_py_agent.agent.plugin_runtime import _verify_builtin_browser_lite_wheel

    return _verify_builtin_browser_lite_wheel(archive_bytes, entry_wheel)


@needs_browser
def test_inspect_to_apply_navigation_wrong_origin_cross_form_and_revocation_teardown(
    background,
    tmp_path,
    browser_lite_bundle,
    dual_sites,
):
    primary_site, secondary_site = dual_sites
    _service, owner, workspace, registry, _client, _repo = _install_and_enable_browser_lite(
        background, tmp_path, browser_lite_bundle,
    )
    try:
        ref, _card_path, _submitted = _setup_im_card(
            background, primary_site.origin + "/signin", authentication="collected",
        )
        scope = tool_approval_scope(background.agent, background.task_id)
        snapshot = registry.runtime_snapshot(run_id=background.task_id)
        open_tool = registry.tools[plugin_tool_name("browser-lite", "open")]
        read_tool = registry.tools[plugin_tool_name("browser-lite", "read")]
        fill_tool = registry.tools[plugin_tool_name("browser-lite", "fill")]

        # 1) 打开不同端口站点（secondary_site），使用绑定 primary_site 的凭据填表 -> inspect 后宿主拦截，错站 0 带秘请求
        cred_use = _HostCredentialUseAdapter(
            background.agent, owner_id=owner.owner_id, actor_id="ou_synthetic", thread_id=scope.thread_id,
        )
        ctx = _invocation_context(snapshot, workspace, credential_use=cred_use)
        sec_before = len(secondary_site.audit_events)
        opened_wrong = open_tool.execute_scoped({"url": secondary_site.origin + "/signin"}, ctx)
        assert opened_wrong.ok
        wrong_origin_fill = fill_tool.execute_scoped(
            {"selector": "#password", "credential_ref": ref, "field": "secret"},
            ctx,
        )
        assert not wrong_origin_fill.ok
        assert wrong_origin_fill.error_code in {"CREDENTIAL_NOT_AUTHORIZED", "CREDENTIAL_INPUT_UNAVAILABLE"}
        assert wrong_origin_fill.effect_outcome == "not_started"
        # secondary_site 只收到过 GET /signin（及 /transform.js），没有任何凭据提交请求
        assert all(ev["method"] == "GET" and not ev["raw_secret_on_wire"] for ev in secondary_site.audit_events[sec_before:])

        # 2) 同源页面但 <form action> 指向另一端口 -> apply DOM 核验拒绝 CROSS_ORIGIN_FORM_ACTION，不填入凭据
        opened_cross = open_tool.execute_scoped({"url": primary_site.origin + "/cross-form"}, ctx)
        assert opened_cross.ok
        cross_fill = fill_tool.execute_scoped(
            {"selector": "#password", "credential_ref": ref, "field": "secret"},
            ctx,
        )
        assert not cross_fill.ok and cross_fill.error_code == "TOOL_PERMISSION_DENIED"
        assert cross_fill.reported_error_code == "CROSS_ORIGIN_FORM_ACTION"
        assert cross_fill.effect_outcome == "not_started"

        # 3) inspect -> apply 中途页面发生导航变化 -> DOCUMENT_CHANGED 拒绝写入，错页未填入任何值
        assert open_tool.execute_scoped({"url": primary_site.origin + "/signin"}, ctx).ok

        def navigate_mid_flight():
            open_tool.execute_scoped({"url": secondary_site.origin + "/signin"}, ctx)

        race_use = _HostCredentialUseAdapter(
            background.agent,
            owner_id=owner.owner_id,
            actor_id="ou_synthetic",
            thread_id=scope.thread_id,
            after_inspect_hook=navigate_mid_flight,
        )
        race_ctx = _invocation_context(snapshot, workspace, credential_use=race_use)
        race_fill = fill_tool.execute_scoped(
            {"selector": "#password", "credential_ref": ref, "field": "secret"},
            race_ctx,
        )
        assert not race_fill.ok and race_fill.error_code == "TOOL_EXECUTION_FAILED"
        assert race_fill.reported_error_code == "DOCUMENT_CHANGED"
        assert race_fill.effect_outcome == "not_started"
        verify_untouched = read_tool.execute_scoped({"selector": "#password"}, ctx)
        assert verify_untouched.ok
        pw_item = json.loads(json.loads(verify_untouched.output)["result"])["items"][0]
        assert pw_item.get("value", "") == ""

        # 4) 凭据成功绑定浏览器会话后被撤销 -> 后续 read 立即关闭隔离 Chromium 进程并删除临时 profile
        reopened = open_tool.execute_scoped({"url": primary_site.origin + "/signin"}, ctx)
        assert reopened.ok
        active_pid = int(json.loads(json.loads(reopened.output)["result"])["browser_pid"])
        ok_fill = fill_tool.execute_scoped(
            {"selector": "#username", "credential_ref": ref, "field": "username"},
            ctx,
        )
        assert ok_fill.ok
        manage_credential(background.agent, ref, operation="revoke")
        after_revoke = read_tool.execute_scoped({}, ctx)
        assert not after_revoke.ok and after_revoke.error_code == "CREDENTIAL_INPUT_UNAVAILABLE"
        assert after_revoke.effect_outcome == "not_started"
        assert gone(active_pid)
        sessions_dir = owner.plugins_dir / "data" / "browser-lite" / "sessions"
        assert not sessions_dir.exists() or not list(sessions_dir.iterdir())
    finally:
        registry.close_mcp_clients()


def test_login_schema_body_encoding_backwards_compatibility():
    fields = credential_fields("login", [{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}])
    legacy = credential_auth_config(
        "login",
        {"url": "https://example.com/login", "body_fields": {"u": "username", "p": "secret"}, "token_path": ["token"]},
        fields,
    )
    explicit_json = credential_auth_config(
        "login",
        {
            "url": "https://example.com/login",
            "body_fields": {"u": "username", "p": "secret"},
            "body_encoding": "json",
            "token_path": ["token"],
        },
        fields,
    )
    assert legacy == explicit_json and "body_encoding" not in legacy
    explicit_form = credential_auth_config(
        "login",
        {
            "url": "https://example.com/login",
            "body_fields": {"u": "username", "p": "secret"},
            "body_encoding": "form",
            "cookies": True,
        },
        fields,
    )
    assert explicit_form["body_encoding"] == "form"
    for invalid in ("xml", "multipart", 123):
        with pytest.raises(ValueError, match="编码方式无效"):
            credential_auth_config(
                "login",
                {"url": "https://example.com/login", "body_fields": {"p": "secret"}, "body_encoding": invalid, "cookies": True},
                fields,
            )


def test_real_http_collected_zero_requests_bearer_json_form_415_redirect_expired_401_and_mfa(
    background,
    server,
    dual_sites,
):
    _primary_site, other_port_site = dual_sites
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    owner = resolve_owner_home(background.agent.home_paths.root)
    scope = tool_approval_scope(background.agent, background.task_id)
    cred_use = _HostCredentialUseAdapter(
        background.agent, owner_id=owner.owner_id, actor_id="ou_synthetic", thread_id=scope.thread_id,
    )
    old_get = server.RequestHandlerClass.do_GET
    old_post = getattr(server.RequestHandlerClass, "do_POST", None)

    state: dict[str, object] = {
        "mode": "normal",
        "hits": [],
        "form_bodies": [],
        "json_bodies": [],
    }

    def do_post(handler):
        length = int(handler.headers.get("Content-Length", "0"))
        raw = handler.rfile.read(length) if length > 0 else b""
        ctype = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        state["hits"].append(("POST", handler.path, ctype))
        if handler.path == "/login-415":
            handler._send(415, "application/json", b'{"error":"unsupported_media_type"}')
            return
        if handler.path == "/login-mfa":
            handler._send(200, "application/json", b'{"mfa_required":true,"challenge_type":"totp"}')
            return
        if handler.path == "/login-401":
            handler._send(401, "application/json", b'{"error":"bad_credentials"}')
            return
        if handler.path == "/login-redirect":
            handler._send(
                307,
                "text/plain",
                b"",
                {"Location": f"{other_port_site.origin}/other-endpoint"},
            )
            return
        if handler.path == "/login-form":
            assert ctype == "application/x-www-form-urlencoded"
            parsed = {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
            state["form_bodies"].append(parsed)
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Set-Cookie", "sid=FormSessionCookie-77; Path=/")
            handler.send_header("Set-Cookie", "csrf=FormCsrf-88; Path=/")
            body = b'{"access_token":"FormAccessToken-99"}'
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        if handler.path == "/login-json":
            assert ctype == "application/json"
            state["json_bodies"].append(json.loads(raw.decode("utf-8")))
            handler._send(200, "application/json", b'{"access_token":"JsonAccessToken-55"}')
            return
        handler._send(404, "text/plain", b"not found")

    def do_get(handler):
        state["hits"].append(("GET", handler.path, handler.headers.get("Authorization", "")))
        if handler.path == "/bearer-data":
            if handler.headers.get("Authorization") != f"Bearer {SYNTHETIC_BEARER_TOKEN}":
                handler._send(401, "text/plain", b"unauthorized")
                return
            echo = f"{PROTECTED_RECORD_MARKER} {quote_plus(SYNTHETIC_BEARER_TOKEN, safe='')}"
            handler._send(200, "text/plain; charset=utf-8", echo.encode())
            return
        if handler.path == "/header-data":
            if handler.headers.get("X-Api-Token") != SYNTHETIC_BEARER_TOKEN:
                handler._send(401, "text/plain", b"unauthorized")
                return
            handler._send(200, "text/plain; charset=utf-8", PROTECTED_RECORD_MARKER.encode())
            return
        if handler.path == "/protected-form":
            if (handler.headers.get("Authorization") != "Bearer FormAccessToken-99"
                    or "sid=FormSessionCookie-77" not in (handler.headers.get("Cookie") or "")
                    or handler.headers.get("X-CSRF-Token") != "FormCsrf-88"):
                handler._send(401, "text/plain", b"unauthorized")
                return
            echo = f"{PROTECTED_RECORD_MARKER} FormAccessToken-99 FormSessionCookie-77"
            handler._send(200, "text/plain; charset=utf-8", echo.encode())
            return
        if handler.path == "/protected-json":
            if handler.headers.get("Authorization") != "Bearer JsonAccessToken-55":
                handler._send(401, "text/plain", b"unauthorized")
                return
            handler._send(200, "text/plain; charset=utf-8", PROTECTED_RECORD_MARKER.encode())
            return
        if handler.path == "/redirect-port":
            handler._send(302, "text/plain", b"", {"Location": f"{other_port_site.origin}/account"})
            return
        handler._send(404, "text/plain", b"not found")

    server.RequestHandlerClass.do_POST = do_post
    server.RequestHandlerClass.do_GET = do_get
    tool = _web_fetch_tool()
    tool.credential_authenticator = lambda r, req, send: credential_authenticated_request(
        background.agent, r, req, send=send, credential_use=cred_use,
    )
    dummy_snapshot = SimpleNamespace(run_id=background.task_id)
    ctx = SimpleNamespace(credential_use=cred_use, runtime_snapshot=dummy_snapshot)

    try:
        # 1) 单字段 collected（password 模式）：HTTP 零请求、零解密，返回 configure(authentication="bearer") 指引
        single_ref, _single_path, _ = _setup_im_card(
            background, origin + "/bearer-data", authentication="collected", collection_mode="password",
        )
        hits_before = len(state["hits"])
        single_pre = tool.execute_scoped({"url": origin + "/bearer-data", "credential_ref": single_ref, "mode": "text"}, ctx)
        assert not single_pre.ok and single_pre.error_code == "CREDENTIAL_BINDING_INVALID"
        assert single_pre.effect_outcome == "not_started"
        assert 'authentication="bearer"' in single_pre.output
        assert len(state["hits"]) == hits_before

        # 2) 显式 bearer 与显式自定义 header
        bearer_ref, _, _ = _setup_im_card(background, origin + "/bearer-data", authentication="bearer")
        bearer_res = tool.execute_scoped({"url": origin + "/bearer-data", "credential_ref": bearer_ref, "mode": "text"}, ctx)
        assert bearer_res.ok and PROTECTED_RECORD_MARKER in bearer_res.output
        assert "[private]" in bearer_res.output
        _assert_zero_secret_variants(bearer_res.output, (SYNTHETIC_BEARER_TOKEN,))

        header_ref, _, _ = _setup_im_card(
            background,
            origin + "/header-data",
            authentication="header",
            header_name="X-Api-Token",
            fields=[{"name": "secret", "label": "令牌"}],
        )
        header_res = tool.execute_scoped({"url": origin + "/header-data", "credential_ref": header_ref, "mode": "text"}, ctx)
        assert header_res.ok and PROTECTED_RECORD_MARKER in header_res.output
        _assert_zero_secret_variants(header_res.output, (SYNTHETIC_BEARER_TOKEN,))

        # 3) 缺省 login（application/json）与显式 body_encoding="form"（application/x-www-form-urlencoded）
        json_ref, _, json_vals = _setup_im_card(
            background,
            origin + "/protected-json",
            authentication="login",
            fields=[{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}],
            auth_config={
                "url": origin + "/login-json",
                "body_fields": {"user": "username", "pass": "secret"},
                "token_path": ["access_token"],
            },
        )
        json_res = tool.execute_scoped({"url": origin + "/protected-json", "credential_ref": json_ref, "mode": "text"}, ctx)
        assert json_res.ok and PROTECTED_RECORD_MARKER in json_res.output
        assert state["json_bodies"] == [{"user": json_vals["username"], "pass": json_vals["secret"]}]

        form_ref, _, form_vals = _setup_im_card(
            background,
            origin + "/protected-form",
            authentication="login",
            fields=[{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}],
            auth_config={
                "url": origin + "/login-form",
                "body_fields": {"user": "username", "pass": "secret"},
                "body_encoding": "form",
                "token_path": ["access_token"],
                "cookies": True,
                "csrf_cookie": "csrf",
            },
        )
        form_res = tool.execute_scoped({"url": origin + "/protected-form", "credential_ref": form_ref, "mode": "text"}, ctx)
        assert form_res.ok and PROTECTED_RECORD_MARKER in form_res.output
        assert "[private]" in form_res.output
        assert state["form_bodies"] == [{"user": form_vals["username"], "pass": form_vals["secret"]}]
        assert "FormAccessToken-99" not in form_res.output and "FormSessionCookie-77" not in form_res.output

        # 4) HTTP 415：保留登录请求已发送事实与原 credential_ref 降级提示，不标记凭据 rejected
        ref_415, path_415, _ = _setup_im_card(
            background,
            origin + "/protected-json",
            authentication="login",
            fields=[{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}],
            auth_config={
                "url": origin + "/login-415",
                "body_fields": {"user": "username", "pass": "secret"},
                "token_path": ["access_token"],
            },
        )
        res_415 = tool.execute_scoped({"url": origin + "/protected-json", "credential_ref": ref_415, "mode": "text"}, ctx)
        assert not res_415.ok and res_415.effect_outcome == "failed"
        assert res_415.result_envelope["http_status"] == 415
        assert res_415.result_envelope["credential_ref"] == ref_415
        assert res_415.result_envelope["login_attempt"]["sent"] is True
        assert ref_415 in res_415.next_action and "browser-lite" in res_415.next_action
        saved_415 = read_json_object_report(path_415, context="fc3.415").payload
        assert saved_415["verification_status"] == "unverified"

        # 5) 跳转到另一端口被 authorization_origin 阻断，另一端口收到 0 请求
        other_before = len(other_port_site.audit_events)
        redir_res = tool.execute_scoped({"url": origin + "/redirect-port", "credential_ref": bearer_ref, "mode": "text"}, ctx)
        assert not redir_res.ok
        assert len(other_port_site.audit_events) == other_before

        # 6) 过期凭据零发包拒绝；401 记录首次核验拒绝且不盲重放写请求；人工 MFA 明确报告阶段且不判错密码
        exp_ref, exp_path, _ = _setup_im_card(background, origin + "/bearer-data", authentication="bearer")
        with _credential_mutation(background.agent, exp_path) as admission:
            exp_row = read_json_object_report(exp_path, context="fc3.exp").payload
            exp_row["ready_expires_at"] = time.time() - 10
            _save_credential_row(admission, exp_path, exp_row)
        hits_pre_exp = len(state["hits"])
        exp_res = tool.execute_scoped({"url": origin + "/bearer-data", "credential_ref": exp_ref, "mode": "text"}, ctx)
        assert not exp_res.ok and exp_res.error_code == "CREDENTIAL_INPUT_UNAVAILABLE"
        assert exp_res.effect_outcome == "not_started"
        assert len(state["hits"]) == hits_pre_exp

        ref_401, path_401, _ = _setup_im_card(
            background,
            origin + "/protected-json",
            authentication="login",
            fields=[{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}],
            auth_config={
                "url": origin + "/login-401",
                "body_fields": {"user": "username", "pass": "secret"},
                "token_path": ["access_token"],
            },
        )
        res_401 = tool.execute_scoped(
            {"url": origin + "/protected-json", "method": "POST", "body": "{}", "credential_ref": ref_401, "mode": "text"},
            ctx,
        )
        assert not res_401.ok
        row_401 = read_json_object_report(path_401, context="fc3.401").payload
        assert row_401["verification_status"] == "rejected"

        ref_mfa, path_mfa, _ = _setup_im_card(
            background,
            origin + "/protected-json",
            authentication="login",
            fields=[{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}],
            auth_config={
                "url": origin + "/login-mfa",
                "body_fields": {"user": "username", "pass": "secret"},
                "token_path": ["access_token"],
            },
        )
        res_mfa = tool.execute_scoped({"url": origin + "/protected-json", "credential_ref": ref_mfa, "mode": "text"}, ctx)
        assert not res_mfa.ok and res_mfa.error_code == "CREDENTIAL_INPUT_UNAVAILABLE"
        assert res_mfa.result_envelope["verification_challenge"]["kind"] == "mfa"
        row_mfa = read_json_object_report(path_mfa, context="fc3.mfa").payload
        assert row_mfa["verification_status"] == "unverified"
    finally:
        if old_post is None:
            delattr(server.RequestHandlerClass, "do_POST")
        else:
            server.RequestHandlerClass.do_POST = old_post
        server.RequestHandlerClass.do_GET = old_get


def test_web_fetch_secret_echo_variants_delegates_to_c5_and_redacts_nested_structures(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def spy_variants(values):
        captured = tuple(values)
        calls.append(captured)
        return _all_secret_variants(captured)

    monkeypatch.setattr(host_output_redaction, "credential_echo_variants", spy_variants, raising=False)
    sample = "Token Space+Plus/Slash=End α"
    variants = _secret_echo_variants((sample,))
    assert calls == [(sample,)]
    b64_std = base64.b64encode(sample.encode("utf-8")).decode("ascii").rstrip("=")
    b64_url = base64.urlsafe_b64encode(sample.encode("utf-8")).decode("ascii").rstrip("=")
    nested = {
        f"key-{quote_plus(sample, safe='')}": [
            f"raw={sample}",
            (f"b64={b64_std}", f"urlb64={b64_url}"),
        ]
    }
    redacted = _redact_secret_echo(nested, (sample,))
    serialized = json.dumps(redacted, ensure_ascii=False)
    assert "[private]" in serialized
    _assert_zero_secret_variants(serialized, (sample,))
