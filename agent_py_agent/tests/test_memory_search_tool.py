"""J9：主模型只读长期记忆检索工具 memory_search（P5-A 缺口 2）。

钉住：开关关时不注册；开着时只读（写参数在执行前被拒、正式记忆与候选账不变）；只查当前 owner 和本轮适用范围；
条数与正文摘录有上限；语义可用时报 semantic、嵌入失败或没有嵌入端时退回关键词并给原因；
不写访问信号（不进自动召回账）；自动召回被抑制的回合不可用；模型不能自己打开开关。
全部用临时 home 与 tests/_hashing_embedder.py，不读真实记忆、不联网。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent import core
from agent_py_agent.agent.capability.memory_search_tool import (
    MEMORY_SEARCH_EXCERPT_CHARS,
    MEMORY_SEARCH_MAX_COUNT,
    MemorySearchTool,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
from agent_py_agent.agent.retrieval.embedding import EmbeddingError
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.parameter_changes import ChangeOrigin, WritePaths, set_parameter
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import USER_SETTINGS_BOUNDARY_KEYS
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

PERSONAL = {"scope_type": "personal", "scope_key": "personal"}


def _agent(tmp_path: Path, *, enabled: bool = True, **kw) -> SimpleAgent:
    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                         enable_memory_search_tool=enabled, **kw)
    return SimpleAgent(config, tmp_path / "repo")


def _remember(agent: SimpleAgent, content: str, *, kind: str = "fact", scope: dict | None = None) -> str:
    record = agent.memory.add("user", content, kind=kind, attributes=dict(scope or PERSONAL))
    return record.entry_id


def _call(agent: SimpleAgent, arguments: dict) -> dict:
    result = execute_registry_test_call(agent.tools, "memory_search", arguments, register_with=agent)
    assert result.ok, result.output
    return json.loads(result.output)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def test_switch_off_keeps_tool_out_of_registry_and_snapshot(tmp_path):
    agent = _agent(tmp_path, enabled=False)
    assert AgentConfig().enable_memory_search_tool is False
    assert "memory_search" not in agent.tools.tools
    result = execute_registry_test_call(agent.tools, "memory_search", {"query": "端口"})
    assert result.ok is False and result.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"


def test_switch_on_registers_read_only_tool_with_closed_schema(tmp_path):
    agent = _agent(tmp_path)
    tool = agent.tools.tools["memory_search"]
    assert isinstance(tool, MemorySearchTool)
    assert tool.runtime_policy.effect_resolver.default_effect == "read_only"
    assert tool.runtime_policy.effect_resolver.by_parameter == ()
    schema = tool.model_spec.input_schema
    assert schema["additionalProperties"] is False and set(schema["properties"]) == {"query", "limit", "kind"}
    assert "memory_search" in agent.tools.runtime_snapshot(run_id="r").available_tool_names


def test_write_parameters_are_rejected_before_the_handler_runs(tmp_path):
    agent = _agent(tmp_path)
    _remember(agent, "社区图书借阅服务使用8080端口")
    memory_file = Path(agent.memory.path)
    before = (_digest(memory_file), _digest(agent.home_paths.owner_memory_candidates_jsonl))
    for extra in ({"action": "add", "content": "新事实"}, {"entry_id": "x", "content": "改写"}, {"remove": True}):
        result = execute_registry_test_call(agent.tools, "memory_search", {"query": "端口", **extra})
        assert result.ok is False and result.error_code == "TOOL_INVALID_ARGUMENTS", extra
    assert (_digest(memory_file), _digest(agent.home_paths.owner_memory_candidates_jsonl)) == before


def test_only_current_scope_entries_with_keyword_mode_and_fallback_reason(tmp_path):
    agent = _agent(tmp_path)
    personal = _remember(agent, "社区图书借阅服务使用8080端口")
    project = _remember(agent, "小说项目的借阅系统端口是9090", kind="project",
                        scope={"scope_type": "project", "scope_key": "project:novel"})
    _remember(agent, "别的公司借阅服务端口7070", scope={"scope_type": "company", "scope_key": "company:other"})
    agent.memory.add("user", "没有范围的旧借阅端口记录6060", kind="fact")

    payload = _call(agent, {"query": "借阅服务端口"})

    # 与自动召回同一范围：personal 加记忆库里实际存在的 project；别的公司与无范围旧记录查不到。
    assert {entry["entry_id"] for entry in payload["entries"]} == {personal, project}
    assert payload["retrieval"]["mode"] == "keyword"
    assert payload["retrieval"]["fallback_reason"] == "semantic_recall_disabled"
    assert payload["retrieval"]["scoped_entries"] == 2
    assert payload["authority"] == "clue_only"
    only_projects = _call(agent, {"query": "借阅服务端口", "kind": "project"})
    assert [entry["entry_id"] for entry in only_projects["entries"]] == [project]
    entry = next(item for item in payload["entries"] if item["entry_id"] == personal)
    assert entry["scope"] == PERSONAL and entry["kind"] == "fact" and entry["updated_at"].endswith("+00:00")


def test_cross_owner_entries_are_unreachable(tmp_path):
    base = _agent(tmp_path, gateway_per_user_owner_scoping=True)
    alice = _resolve_request_agent(base, {"user_id": "alice", "metadata": {"user_id": "alice", "channel": "feishu"}})
    bob = _resolve_request_agent(base, {"user_id": "bob", "metadata": {"user_id": "bob", "channel": "feishu"}})
    assert alice is not bob
    secret = _remember(alice, "公司A的支付系统迁移预算五十万")

    assert [entry["entry_id"] for entry in _call(alice, {"query": "支付系统迁移预算"})["entries"]] == [secret]
    bob_payload = _call(bob, {"query": "支付系统迁移预算"})
    assert bob_payload["entries"] == [] and bob_payload["retrieval"]["scoped_entries"] == 0


def test_limit_and_excerpt_are_bounded(tmp_path):
    agent = _agent(tmp_path)
    for index in range(MEMORY_SEARCH_MAX_COUNT + 3):
        _remember(agent, f"部署清单第{index}项：服务器编号{index}需要先停机")
    long_id = _remember(agent, "部署清单长说明：" + "停机步骤" * 200)

    assert len(_call(agent, {"query": "部署清单停机"})["entries"]) == 5
    assert len(_call(agent, {"query": "部署清单停机", "limit": MEMORY_SEARCH_MAX_COUNT})["entries"]) == MEMORY_SEARCH_MAX_COUNT
    too_many = execute_registry_test_call(agent.tools, "memory_search", {"query": "部署", "limit": MEMORY_SEARCH_MAX_COUNT + 1})
    assert too_many.ok is False and too_many.error_code == "TOOL_INVALID_ARGUMENTS"
    # 处理函数自身也收口：绕过 schema 直接调用时条数仍不超过上限。
    direct = json.loads(MemorySearchTool(agent).execute({"query": "部署清单停机", "limit": 50}).output)
    assert len(direct["entries"]) == MEMORY_SEARCH_MAX_COUNT
    long_entry = next(entry for entry in _call(agent, {"query": "部署清单长说明停机步骤", "limit": 10})["entries"]
                      if entry["entry_id"] == long_id)
    assert len(long_entry["excerpt"]) == MEMORY_SEARCH_EXCERPT_CHARS
    assert long_entry["excerpt_truncated"] is True and long_entry["content_chars"] == len("部署清单长说明：") + 800


def test_semantic_mode_and_keyword_fallback_when_embedding_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_build_memory_embedder", lambda agent, **kwargs: LocalHashingEmbedder(dim=128))
    # P14 起语义通道要能由同一客户端算出空间身份（档案编号 + 协议 + 端点 + 模型），所以配上档案编号。
    agent = _agent(tmp_path, memory_semantic_recall=True, embedding_model_profile="embed-test")
    fact = _remember(agent, "周会固定在星期三下午三点")

    semantic = _call(agent, {"query": "周会什么时候开"})
    assert semantic["retrieval"]["mode"] == "semantic" and semantic["retrieval"]["fallback_reason"] == ""
    assert fact in {entry["entry_id"] for entry in semantic["entries"]}

    class _Failing(LocalHashingEmbedder):
        def embed(self, texts):
            raise EmbeddingError("endpoint down")

    monkeypatch.setattr(agent.memory, "_embedder", _Failing(dim=128))
    fallback = _call(agent, {"query": "周会星期三"})
    assert fallback["retrieval"]["mode"] == "keyword"
    assert fallback["retrieval"]["fallback_reason"] == "embedding_failed"
    assert [entry["entry_id"] for entry in fallback["entries"]] == [fact]


def test_closed_semantic_channel_reports_the_structured_reason(tmp_path, monkeypatch):
    # 开了语义召回、客户端也建得出来，但没有档案编号 → 空间身份不可用，通道整条关闭；原因如实来自结构化诊断码。
    monkeypatch.setattr(core, "_build_memory_embedder", lambda agent, **kwargs: LocalHashingEmbedder(dim=128))
    agent = _agent(tmp_path, memory_semantic_recall=True)
    _remember(agent, "周会固定在星期三下午三点")

    retrieval = _call(agent, {"query": "周会星期三"})["retrieval"]
    assert retrieval["mode"] == "keyword"
    assert retrieval["fallback_reason"] == "embedding_identity_unavailable"
    assert retrieval["semantic_recall"]["error_code"] == "MEMORY_EMBEDDING_IDENTITY_UNAVAILABLE"


def test_results_do_not_enter_recall_access_accounting(tmp_path):
    agent = _agent(tmp_path)
    fact = _remember(agent, "家里的路由器管理地址是192.168.1.1")
    agent.memory.flush_access_events()

    assert [entry["entry_id"] for entry in _call(agent, {"query": "路由器管理地址"})["entries"]] == [fact]
    assert agent.memory.flush_access_events() == 0
    # 对照：自动召回入口会登记访问信号，说明上面的 0 不是因为检索没命中。
    agent.memory.search_scoped("路由器管理地址", 3, lambda record: True)
    assert agent.memory.flush_access_events() == 1


def test_unavailable_when_auto_recall_is_suppressed(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    _remember(agent, "社区图书借阅服务使用8080端口")
    for context_scope in ("task_local", "control_plane"):
        monkeypatch.setattr(agent, "_current_run_params",
                            SimpleNamespace(context_scope=context_scope, task_id="", task_attributes={}), raising=False)
        snapshot = agent.tools.runtime_snapshot(run_id="r")
        assert "memory_search" not in snapshot.available_tool_names
        assert ("memory_search", "TOOL_UNAVAILABLE") in {item[:2] for item in snapshot.unavailable_tools}
    monkeypatch.setattr(agent, "_current_run_params",
                        SimpleNamespace(context_scope="default", task_id="", task_attributes={}), raising=False)
    assert agent.tools.tools["memory_search"].availability().available is True
    monkeypatch.setattr(agent, "owner_policy", SimpleNamespace(memory_enabled=False), raising=False)
    assert agent.tools.tools["memory_search"].availability().available is False


def test_switch_is_a_boundary_the_model_cannot_flip(tmp_path):
    spec = parameter_registry()["enable_memory_search_tool"]
    assert spec.default is False and spec.writable is False
    assert "enable_memory_search_tool" in USER_SETTINGS_BOUNDARY_KEYS
    user_config = tmp_path / "user.yaml"
    user_config.write_text("", encoding="utf-8")
    report = set_parameter("enable_memory_search_tool", True, paths=WritePaths(user_path=user_config),
                           origin=ChangeOrigin("model"))
    assert report["ok"] is False and report["code"] == "PARAMETER_BOUNDARY"
    assert user_config.read_text(encoding="utf-8") == ""
