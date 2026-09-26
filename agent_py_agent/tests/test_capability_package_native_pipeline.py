"""模型替身沿真实原生工具循环使用包资源；不发网络请求，不作为自然召回证据。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend
from agent_py_agent.tests.test_capability_package_task_refs import _agent


# LLM: 替身只产结构化模型响应；来源引用必须从上轮真实工具结果读取，不允许测试替模型预填文件或 pin。
# 类用途: 回放读包、按引用落盘、收口三轮响应，验证宿主装配与原生历史的完整连接。
class _PackagePipelineBackend(_TestNativeBackend):
    name = "package_pipeline_fixture"
    model_name = "local-fixture"

    def __init__(self, target):
        self.target = target
        self.requests = []
        self.source_ref = None

    def generate(self, prompt, on_chunk=None, **kwargs):
        self.requests.append(kwargs)
        index = len(self.requests)
        if index == 1:
            return self._call("read-package", "skill_search", {
                "action": "get", "package_id": "story-a", "resource_path": "methods/SKILL.md",
            })
        results = {block["tool_use_id"]: block for row in kwargs["messages"]
                   for block in row["content"] if block["type"] == "tool_result"}
        if index == 2:
            result = results["read-package"]
            assert not result["is_error"], result["content"]
            content = result["content"]
            payload, _ = json.JSONDecoder().raw_decode(content[content.index("{"):])
            self.source_ref = payload["source_ref"]
            return self._call("copy-package-resource", "write_file", {
                "path": str(self.target), "source_ref": self.source_ref,
            })
        assert index == 3, "完整闭环不应靠额外模型重试掩盖接线错误"
        assert not results["copy-package-resource"]["is_error"], results["copy-package-resource"]["content"]
        return ModelResponse(text="已按原资源生成文件。", backend=self.name)

    def _call(self, call_id, name, arguments):
        return ModelResponse(text="", backend=self.name,
                             tool_use_blocks=[{"id": call_id, "name": name, "input": arguments}])


def test_native_model_pipeline_binds_then_materializes_original_resource(tmp_path):
    agent, _store, entries = _agent(tmp_path)
    owner_workspace = resolve_owner_home(agent.home_paths.root).workspace_dir / "pipeline"
    owner_workspace.mkdir(parents=True)
    agent = SimpleAgent(agent.config, owner_workspace)
    target = agent.root / "materialized-method.md"
    backend = _PackagePipelineBackend(target)
    agent.backend = backend
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "pipeline",
    })
    result = agent.run("把现有故事方法整理到工作区。", save=False,
                       allowed_tools=["skill_search", "write_file"],
                       task_attributes={"conversation_thread_id": thread.thread_id},
                       context_scope="conversation")

    assert result.response == "已按原资源生成文件。"
    assert target.read_bytes() == b"story-a"
    tasks = agent.conversation_store.tasks.list(thread.thread_id)
    assert len(tasks) == 1
    assert tasks[0].skill_snapshot_refs[0]["activation_id"] == entries[0].activation_id
    assert backend.source_ref["resource_path"] == "methods/SKILL.md"
    write_schema = next(row for row in backend.requests[0]["tools"] if row["name"] == "write_file")
    assert "source_ref" in write_schema["input_schema"]["properties"]
    operations = result.operation_verification["operations"]
    assert any(row["tool"] == "write_file" and row["verification_status"] == "succeeded" for row in operations)
    assert not agent.current_skill_snapshot().resolve("methods/SKILL.md")


@pytest.mark.parametrize("preview_chars", [0, 200, 1800])
def test_externalized_package_page_exposes_exact_copy_reference_without_rewriting_body(tmp_path, preview_chars):
    raw = b"# resource\r\n" + b"do_not_rewrite = True\r\n" * 800 + b"# final invariant\r\n"
    agent, _store, _entries = _agent(tmp_path, method_body=raw)
    workspace = resolve_owner_home(agent.home_paths.root).workspace_dir / "large-resource"
    workspace.mkdir(parents=True)
    agent = SimpleAgent(replace(agent.config, tool_output_externalize_min_chars=100,
                                tool_output_preview_chars=preview_chars, tool_read_max_chars=5000), workspace)
    target = agent.root / "original-resource.py"
    backend = _PackagePipelineBackend(target)
    agent.backend = backend
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "large-resource",
    })
    result = agent.run("将已有资源原样整理到当前工作区。", save=False,
                       allowed_tools=["skill_search", "write_file"],
                       task_attributes={"conversation_thread_id": thread.thread_id},
                       context_scope="conversation")

    assert result.response == "已按原资源生成文件。"
    assert target.read_bytes() == raw
    model_results = [block for row in backend.requests[1]["messages"] for block in row["content"]
                     if block["type"] == "tool_result" and block["tool_use_id"] == "read-package"]
    rendered = model_results[0]["content"]
    payload, _ = json.JSONDecoder().raw_decode(rendered[rendered.index("{"):])
    assert payload["source_ref"] == backend.source_ref
    assert payload["body_preview_complete"] is False
    assert len(payload["body_preview"]) <= preview_chars
    assert payload["has_more"] is True and payload["continuation"]["offset"] == 5000
    assert "output_scoped_call_id" in rendered
    assert "# final invariant" not in rendered
