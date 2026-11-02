# LLM: 只用 pytest 临时 home、两个合成普通 owner 的原 SimpleAgent/Registry/Executor 与本机随机端口服务；
#   私网授权、网络安全门与 owner 路径墙全部走原实现，不替换解析、抓取、产物保存或读文件逻辑。
# 模块用途: 回归 P1-R9-26——owner 注册表把网页产物落到各自 owner 根的 artifacts/web，独立构造仍走原默认目录。
from __future__ import annotations

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.web import WebFetchTool
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

# 正文远超 256 字符的最小预览；二进制含 NUL 与全部字节值，按字节和摘要核对。
LONG_TEXT = "".join(f"第{n:03d}行 合成网页正文\n" for n in range(120))
BINARY = bytes(range(256)) * 300 + b"\x00\xff tail"
_ROUTES = {
    "/text": (LONG_TEXT.encode("utf-8"), "text/plain; charset=utf-8"),
    "/blob": (BINARY, "application/octet-stream"),
}
# 私网授权只经调用写边界进入原调用副本，与生产逐调用授权同一路径。
_GRANT = {"allowed_private_hosts": ["127.0.0.1"], "allow_private_resolution": True}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        body, content_type = _ROUTES.get(self.path, (b"missing", "text/plain"))
        self.send_response(200 if self.path in _ROUTES else 404)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# 函数用途: 在本机随机端口起一个只服务固定合成内容的 HTTP 服务。
@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    thread.join(2)
    srv.server_close()


# 函数用途: 构造一个普通 owner 的原 Agent，工具表由原 core 装配。
def _owner(tmp_path: Path, user_id: str) -> SimpleAgent:
    return SimpleAgent(AgentConfig(
        model_backend="echo", enable_plugins=False, my_agent_home=str(tmp_path / "home"),
        my_agent_owner_provider="feishu", my_agent_owner_kind="user", my_agent_owner_id=user_id,
        access_mode="workspace-write",
    ), tmp_path / f"workspace-{user_id}")


# 函数用途: 返回该 owner 的规范网页产物目录。
def _web_root(agent: SimpleAgent) -> Path:
    return Path(agent.home_paths.owner_home_dir) / "artifacts" / "web"


# 函数用途: 经原 ToolCall/Registry/Executor 入口执行一次工具调用。
def _call(agent, tool_name, arguments, call_id, *, registry=None, boundary=None):
    return execute_registry_test_call(registry or agent.tools, tool_name, arguments, write_boundary=boundary,
                                      register_with=agent, call_id=call_id)


# LLM: 两个普通 owner 抓同一页：各自产物落在自己的 owner 根，预览只有 256 字符而产物保留全文，
#   本人经原 read_file 读回全文，另一 owner 读同一路径被 owner 墙拒绝。
# 函数用途: 验证 owner 归属、预览之外的全文与跨 owner 拒绝。
def test_two_owners_keep_full_text_under_their_own_root(tmp_path, server):
    alice, bob = _owner(tmp_path, "ou_alice"), _owner(tmp_path, "ou_bob")
    pages = {}
    for name, agent in (("alice", alice), ("bob", bob)):
        result = _call(agent, "web_fetch", {"urls": [f"{server}/text"], "max_chars": 256}, f"{name}-text",
                       boundary=_GRANT)
        assert result.ok, result.output
        pages[name] = result.metadata["handler_details"]["pages"][0]

    alice_ref, bob_ref = Path(pages["alice"]["artifact_ref"]), Path(pages["bob"]["artifact_ref"])
    assert alice_ref.parent == _web_root(alice) and bob_ref.parent == _web_root(bob)
    assert len(pages["alice"]["preview"]) == 256 < len(LONG_TEXT)
    assert alice_ref.read_text(encoding="utf-8") == bob_ref.read_text(encoding="utf-8") == LONG_TEXT
    assert pages["alice"]["content_hash"] == hashlib.sha256(LONG_TEXT.encode("utf-8")).hexdigest()

    read_back = _call(alice, "read_file", {"path": str(alice_ref)}, "alice-read")
    assert read_back.ok, read_back.output
    assert LONG_TEXT.splitlines()[-1] in read_back.output
    denied = _call(bob, "read_file", {"path": str(alice_ref)}, "bob-read")
    assert (denied.ok, denied.error_code) == (False, "PATH_CROSS_OWNER_BLOCKED")


# LLM: 访问策略副本重建同一注册参数，调用副本浅拷贝原工具；两层副本都必须保持同一 owner 根，
#   非文本响应按原字节落盘，摘要与长度如实。
# 函数用途: 验证访问策略副本与调用副本保持根，以及非文本字节与摘要。
def test_nontext_bytes_and_hash_survive_access_policy_copy(tmp_path, server):
    alice = _owner(tmp_path, "ou_alice")
    owner_root = str(alice.home_paths.owner_home_dir)
    view = alice.tools.with_access_policy(access_mode="restricted", path_access_mode=alice.tools.path_access_mode,
                                          owner_scope_root=owner_root)
    assert view is not alice.tools
    assert view.tools["web_fetch"].artifact_root == alice.tools.tools["web_fetch"].artifact_root == _web_root(alice)

    result = _call(alice, "web_fetch", {"url": f"{server}/blob"}, "alice-blob", registry=view, boundary=_GRANT)

    assert result.ok, result.output
    artifact = result.metadata["handler_details"]["artifact"]
    saved = Path(artifact["path"])
    assert saved.parent == _web_root(alice)
    assert saved.read_bytes() == BINARY
    assert (artifact["sha256"], artifact["bytes"]) == (hashlib.sha256(BINARY).hexdigest(), len(BINARY))


# LLM: 进程级环境覆盖只影响独立构造；owner 注册表不回落到它。未授权私网的调用仍被原网络门拒绝且不落产物。
# 函数用途: 验证独立构造兼容、owner 不回落环境覆盖与网络安全门保持。
def test_standalone_default_kept_and_owner_ignores_process_override(tmp_path, server, monkeypatch):
    override = tmp_path / "process-wide-web"
    monkeypatch.setenv("MY_AGENT_WEB_ARTIFACT_ROOT", str(override))
    assert WebFetchTool(max_chars=1000, timeout=5).artifact_root == override
    alice = _owner(tmp_path, "ou_alice")

    blocked = _call(alice, "web_fetch", {"url": f"{server}/blob"}, "alice-no-grant")
    assert not blocked.ok and blocked.error_code
    assert not _web_root(alice).exists()

    granted = _call(alice, "web_fetch", {"url": f"{server}/blob"}, "alice-grant", boundary=_GRANT)
    assert granted.ok, granted.output
    assert Path(granted.metadata["handler_details"]["artifact"]["path"]).parent == _web_root(alice)
    assert not override.exists()
