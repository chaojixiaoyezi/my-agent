"""S7 合同单测：嵌入用量与召回方式的进程内计数，以及 /model vector 的“本次启动以来”几行。

来源：3a 2026-10-02。goal 的 S6 要在生产核对“召回真的走 semantic”和“嵌入次数、token 与预估相符”，但两个嵌入客户端直接发
HTTP、没有计数，召回方式只在 memory_search 的工具正文里。做法：每进程一份内存计数（不落盘、不加开关、不进 model call ledger），
按用途（记忆写入、召回、重建、工具检索）记请求次数、条数、失败和供应商回报的 token（没回报就记“未回报”，不估算）；召回方式按
semantic/keyword/none 计数并留最近一次的原因码；管理员的 /model vector（TUI 与 IM 同一份）显示这些数字，不含正文。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \\
        agent_py_agent/tests/test_embedding_usage.py
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.retrieval.embedding import (
    EmbeddingError,
    MiniMaxEmbedder,
    OpenAICompatibleEmbedder,
)
from agent_py_agent.agent.retrieval.embedding_usage import (
    EMBEDDING_USAGE,
    counted_as,
    embedding_purpose,
)
from agent_py_agent.tests.test_embedding_selection import (  # noqa: F401  （复用 fixture）
    _add,
    _member,
    host,
)

MARKER = "正文标记-绝不能出现在回执里-7f3a"


# 函数用途: 按文本内容给一个确定的 8 维向量（同一文本同一向量），让桩服务像真实嵌入端一样稳定。
def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [float(byte) + 1.0 for byte in digest[:8]]


# 函数用途: 本机嵌入桩服务：OpenAI 兼容与 MiniMax 原生两种协议；mode 控制回报 token、不回报或报 500。
class _Stub(BaseHTTPRequestHandler):
    mode = "usage"
    calls: list[int] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        texts = body.get("input") or body.get("texts") or []
        _Stub.calls.append(len(texts))
        if _Stub.mode == "fail":
            self.send_response(500)
            self.end_headers()
            return
        vectors = [_vector(text) for text in texts]
        if "texts" in body:
            payload: dict[str, object] = {"vectors": vectors, "base_resp": {"status_code": 0}}
            if _Stub.mode == "usage":
                payload["total_tokens"] = 5 * len(texts)
        else:
            payload = {"data": [{"embedding": vector} for vector in vectors]}
            if _Stub.mode == "usage":
                payload["usage"] = {"prompt_tokens": 7 * len(texts), "total_tokens": 7 * len(texts)}
            elif _Stub.mode == "prompt_only":
                payload["usage"] = {"prompt_tokens": 3 * len(texts)}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    _Stub.mode, _Stub.calls = "usage", []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    EMBEDDING_USAGE.reset()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    EMBEDDING_USAGE.reset()


# 函数用途: 读一种用途的计数行。
def _row(purpose: str) -> dict:
    return EMBEDDING_USAGE.snapshot()["embedding"][purpose]


def test_each_purpose_counts_requests_texts_failures_and_reported_tokens(stub):
    openai = OpenAICompatibleEmbedder(api_base=stub, model="text-embedding-x", api_key="k")
    minimax = MiniMaxEmbedder(api_base=stub, model="embo-01", api_key="k")

    with embedding_purpose("memory_write"):
        openai.embed(["a", "b"])
    with embedding_purpose("memory_recall"):
        minimax.embed(["q"])
    openai.embed(["unlabeled"])
    assert openai.embed([]) == [], "空列表不发请求，也不计数"

    assert _row("memory_write") == {"requests": 1, "texts": 2, "failures": 0, "tokens": 14, "tokens_unreported_requests": 0}
    assert _row("memory_recall") == {"requests": 1, "texts": 1, "failures": 0, "tokens": 5, "tokens_unreported_requests": 0}
    assert _row("other")["requests"] == 1, "没标注用途的归 other，不猜"
    assert _Stub.calls == [2, 1, 1]


def test_unreported_tokens_are_not_estimated(stub):
    _Stub.mode = "none"
    with embedding_purpose("memory_rebuild"):
        OpenAICompatibleEmbedder(api_base=stub, model="m").embed(["x"])
        MiniMaxEmbedder(api_base=stub).embed(["y", "z"])

    assert _row("memory_rebuild") == {"requests": 2, "texts": 3, "failures": 0, "tokens": 0, "tokens_unreported_requests": 2}


def test_openai_compatible_falls_back_to_prompt_tokens_when_total_is_missing(stub):
    _Stub.mode = "prompt_only"
    with embedding_purpose("tool_retrieval"):
        OpenAICompatibleEmbedder(api_base=stub, model="m").embed(["a", "b"])
    assert _row("tool_retrieval") == {"requests": 1, "texts": 2, "failures": 0, "tokens": 6, "tokens_unreported_requests": 0}, \
        "只回报 prompt_tokens 也算回报，不当成未回报"


def test_failed_requests_count_and_still_raise(stub):
    _Stub.mode = "fail"
    with embedding_purpose("tool_retrieval"), pytest.raises(EmbeddingError):
        OpenAICompatibleEmbedder(api_base=stub, model="m").embed(["x", "y"])
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    with embedding_purpose("tool_retrieval"), pytest.raises(EmbeddingError):
        MiniMaxEmbedder(api_base=f"http://127.0.0.1:{port}/v1").embed(["x"])

    assert _row("tool_retrieval") == {"requests": 2, "texts": 3, "failures": 2, "tokens": 0, "tokens_unreported_requests": 0}


def test_counted_as_labels_a_method_and_restores_the_outer_purpose(stub):
    class Worker:
        def __init__(self, embedder):
            self.embedder = embedder

        @counted_as("tool_retrieval")
        def work(self):
            return self.embedder.embed(["t"])

    embedder = OpenAICompatibleEmbedder(api_base=stub, model="m")
    with embedding_purpose("memory_write"):
        Worker(embedder).work()
        embedder.embed(["after"])

    assert _row("tool_retrieval")["requests"] == 1 and _row("memory_write")["requests"] == 1


# 函数用途: 建一个真实 SimpleAgent，记忆语义召回接到本机桩服务（生产接线：组合根、JsonlMemory、向量库身份都是真的）。
def _agent(tmp_path, monkeypatch, api_base: str):
    from agent_py_agent.agent import core
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    monkeypatch.setattr(core, "_build_memory_embedder",
                        lambda agent, **kwargs: OpenAICompatibleEmbedder(api_base=api_base, model="stub-embed", api_key="k"))
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), memory_semantic_recall=True,
                                   embedding_model_profile="stub-profile"), tmp_path / "work")


def test_real_memory_write_recall_and_rebuild_are_counted_by_purpose(tmp_path, monkeypatch, stub):
    agent = _agent(tmp_path, monkeypatch, stub)

    agent.memory.add("user", f"客户要求本季度完成支付迁移 {MARKER}")
    hits = agent.memory.search_scoped("付款模块什么时候搬完", 2, lambda _record: True)  # 生产自动召回走的入口
    rebuilt = agent.memory.rebuild_vectors()

    usage = EMBEDDING_USAGE.snapshot()
    assert usage["embedding"]["memory_write"]["requests"] == 1 and usage["embedding"]["memory_write"]["tokens"] == 7
    assert usage["embedding"]["memory_recall"]["requests"] >= 1, "召回至少嵌一次查询"
    assert usage["embedding"]["memory_rebuild"]["requests"] == rebuilt["rebuilt"] == 1
    assert usage["embedding"]["other"]["requests"] == 0, "生产路径的每个嵌入请求都有用途"
    total = sum(row["requests"] for row in usage["embedding"].values())
    assert total == len(_Stub.calls), "计数与桩服务实际收到的请求一一对应"
    assert hits and usage["retrieval"]["semantic"] >= 1 and usage["retrieval"]["last_mode"] == "semantic"


def test_retrieval_modes_keyword_and_none_with_reason(tmp_path, monkeypatch, stub):
    agent = _agent(tmp_path, monkeypatch, stub)
    agent.memory.search_scoped("还没有任何记忆", 2, lambda _record: True)
    assert EMBEDDING_USAGE.snapshot()["retrieval"]["none"] == 1

    agent.memory.add("user", "团建定在下周五")
    _Stub.mode = "fail"
    agent.memory.search_scoped("团建什么时候", 2, lambda _record: True)

    retrieval = EMBEDDING_USAGE.snapshot()["retrieval"]
    assert (retrieval["keyword"], retrieval["last_mode"], retrieval["last_fallback_reason"]) == (1, "keyword", "embedding_failed")


def test_unscoped_recall_counts_one_retrieval_mode_per_call(tmp_path, monkeypatch, stub):
    """Gateway 与 IM 的 /memory 搜索走 agent.recall → JsonlMemory.search（不带作用域）。真实对账（embo-01）发现这条路径的召回
    嵌入有计数、召回方式没有（召回 2 次请求对着召回方式 0 次）；现在每次检索记一次方式，和召回请求一一对上。"""
    agent = _agent(tmp_path, monkeypatch, stub)
    agent.memory.add("user", f"项目代号青柠的发布窗口是每周四 {MARKER}")
    agent.recall("青柠 发布窗口", 3)
    agent.recall("每周四发布的是哪个项目", 3)

    usage = EMBEDDING_USAGE.snapshot()
    assert usage["embedding"]["memory_recall"]["requests"] == 2
    assert (usage["retrieval"]["semantic"], usage["retrieval"]["keyword"], usage["retrieval"]["none"]) == (2, 0, 0)

    _Stub.mode = "fail"
    agent.recall("青柠", 3)
    retrieval = EMBEDDING_USAGE.snapshot()["retrieval"]
    assert (retrieval["keyword"], retrieval["last_mode"], retrieval["last_fallback_reason"]) == (1, "keyword", "embedding_failed")


def test_unscoped_recall_without_semantic_counts_keyword_with_reason(tmp_path, stub):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path / "work")
    agent.memory.add("user", "团建定在下周五")
    agent.recall("团建", 3)

    retrieval = EMBEDDING_USAGE.snapshot()["retrieval"]
    assert (retrieval["keyword"], retrieval["semantic"], retrieval["last_mode"]) == (1, 0, "keyword")
    assert retrieval["last_fallback_reason"], "没有嵌入端时带上结构化原因（同 scoped 检索的口径）"
    assert _Stub.calls == [], "没有语义召回时一次嵌入请求都不发"


def test_tool_semantic_search_counts_as_tool_retrieval(stub):
    from agent_py_agent.agent.tooling.models import ToolModelSpec, VectorToolSearchProvider

    provider = VectorToolSearchProvider(enabled=True, embedder=OpenAICompatibleEmbedder(api_base=stub, model="m"))
    specs = [ToolModelSpec(name=name, description=f"{name} tool", input_schema={"type": "object"}) for name in ("a", "b")]

    provider.search("find a", specs, limit=2)

    assert _row("tool_retrieval")["requests"] == 2 and _row("tool_retrieval")["texts"] == 3, "工具说明一批加查询一次"


# ---------------------------------------------------------------- /model vector 展示（TUI 与 IM 同一份）


# 函数用途: 先在本进程里产生一些用量（含带标记的正文），再按 IM 文字和 TUI 发给 Gateway 的文字各执行一次 /model vector。
def _vector_views(owner_host, monkeypatch, stub):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import model_profile_service
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
    )
    from agent_py_agent.cli.chat_parts.control_runtime import _command_text

    with embedding_purpose("memory_write"):
        OpenAICompatibleEmbedder(api_base=stub, model="m").embed([f"记一条 {MARKER}"])
    _Stub.mode = "none"
    with embedding_purpose("memory_recall"):
        MiniMaxEmbedder(api_base=stub).embed([f"查一下 {MARKER}"])
    EMBEDDING_USAGE.record_retrieval("semantic", "")
    monkeypatch.setattr(model_profile_service, "_scoped_model_host",
                        lambda _agent, _scope: (owner_host, SimpleNamespace(thread_id="t1")))
    im_command = parse_conversation_control("/model vector", reject_unknown_slash=True)
    tui_text = _command_text(parse_conversation_control("/model vector", reject_unknown_slash=True))
    tui_command = parse_conversation_control(tui_text, reject_unknown_slash=True)
    return [execute_gateway_conversation_control(owner_host, None, command, None) for command in (im_command, tui_command)]


def test_admin_vector_view_shows_usage_numbers_without_any_text(host, monkeypatch, stub):  # noqa: F811  （host 是复用的 fixture）
    _add(host)
    views = _vector_views(host, monkeypatch, stub)

    assert views[0].message == views[1].message, "TUI 与 IM 同一份结果"
    message = views[0].message
    assert "本次 Gateway 启动以来" in message
    assert "嵌入·记忆写入：请求 1 次、1 条，失败 0 次，token 7" in message
    assert "嵌入·召回：请求 1 次、1 条，失败 0 次，token 未回报" in message
    assert "嵌入·重建：请求 0 次" in message and "嵌入·工具检索：请求 0 次" in message
    assert "召回方式：semantic 1 次，keyword 0 次，none 0 次；最近一次 semantic，降级原因 无" in message
    assert MARKER not in message and "嵌入·其他" not in message


def test_member_vector_view_does_not_show_process_wide_usage(host, monkeypatch, stub):  # noqa: F811  （host 是复用的 fixture）
    _add(_member(host))
    views = _vector_views(host, monkeypatch, stub)

    assert all("本次 Gateway 启动以来" not in view.message and "只能查看" in view.message for view in views)


# ---------------------------------------------------------------- 日志去重（S7 顺带修）


def test_profile_unavailable_warning_is_logged_once_per_owner_and_reason(monkeypatch, caplog):
    import logging

    from agent_py_agent.agent import core
    from agent_py_agent.agent.backends.errors import ModelNotConfiguredError
    from agent_py_agent.agent.settings.config import AgentConfig

    reasons = iter(["profile_not_found"] * 3 + ["profile_not_found", "credential_missing"])

    def unavailable(agent):
        raise ModelNotConfiguredError(profile_id="p-1", profile_reason=next(reasons))

    monkeypatch.setattr(core, "_embedding_client", unavailable)
    monkeypatch.setattr(core, "_PROFILE_WARNINGS_SEEN", set())

    def build(owner):
        agent = SimpleNamespace(config=AgentConfig(memory_semantic_recall=True), home_paths=SimpleNamespace(owner_id=owner))
        status: dict[str, str] = {}
        assert core._build_memory_embedder(agent, diagnostics=status) is None
        return status

    with caplog.at_level(logging.WARNING, logger=core.__name__):
        statuses = [build("owner-a"), build("owner-a"), build("owner-a"), build("owner-b"), build("owner-b")]
    lines = [record.getMessage() for record in caplog.records if "语义记忆档案不可用" in record.getMessage()]
    assert lines == ["语义记忆档案不可用（profile_not_found）；当前使用关键词召回"] * 2 + \
        ["语义记忆档案不可用（credential_missing）；当前使用关键词召回"], "同一 (owner, 原因) 只打一次；换 owner 或换原因各打一次"
    assert all(status["error_code"] == "MEMORY_EMBEDDING_PROFILE_UNAVAILABLE" for status in statuses), "结构化诊断每次照写"
