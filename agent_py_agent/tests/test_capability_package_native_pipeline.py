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


# LLM: 替身只消费真实模型回执里的归档/续页参数；不读取宿主路径或自行调用reader，最后沿原source_ref物化。
# 类用途: 在原生循环里取回截短的资源页，再复制原资源，核对读取引用和复制引用各自有效。
class _PackageArchiveReadBackend(_PackagePipelineBackend):
    # LLM: 状态只服务本地替身响应序列，不参与产品身份、归档或包授权。
    # 函数用途: 保存模型实际取得的窗口与逻辑引用，供测试核对完整读取结果。
    def __init__(self, target):
        super().__init__(target)
        self.archive_ref = ""
        self.read_count = 0
        self.page_parts = []
        self.restored_page = None

    # LLM: 所有读取/写入均返回结构化工具请求，由真实宿主执行；请求上限只防替身失控，不替产品补结果。
    # 函数用途: 按工具提供的原锚点和next_read分页，完整读回当前资源页后再申请原样复制。
    def generate(self, prompt, on_chunk=None, **kwargs):
        self.requests.append(kwargs)
        assert len(self.requests) <= 9
        if len(self.requests) == 1:
            return self._call("read-package", "skill_search", {
                "action": "get", "package_id": "story-a", "resource_path": "methods/SKILL.md",
            })
        results = {block["tool_use_id"]: block for row in kwargs["messages"]
                   for block in row["content"] if block["type"] == "tool_result"}
        if "copy-package-resource" in results:
            assert not results["copy-package-resource"]["is_error"]
            return ModelResponse(text="已读回资源页并按原资源生成文件。", backend=self.name)
        if len(self.requests) == 2:
            content = results["read-package"]["content"]
            payload, _ = json.JSONDecoder().raw_decode(content[content.index("{"):])
            assert payload["body_preview_complete"] is False
            assert "[tool-result-refs]" not in content
            hint_line = next(line for line in content.splitlines() if line.startswith("- read_artifact_hint: "))
            hint = json.loads(hint_line.split(": ", 1)[1])
            assert hint.pop("tool") == "read_artifact"
            self.archive_ref = hint["artifact_ref"]
            self.source_ref = payload["source_ref"]
        else:
            result = results[f"archive-page-{self.read_count}"]
            assert not result["is_error"], result["content"]
            content = result["content"]
            payload, _ = json.JSONDecoder().raw_decode(content[content.index("{"):])
            assert payload["artifact_ref"] == self.archive_ref
            assert "tool-output-archive-anchor" not in content
            self.page_parts.append(payload["content"])
            if not payload["has_more_after"]:
                self.restored_page = json.loads("".join(self.page_parts))
                assert self.restored_page["source_ref"] == self.source_ref
                return self._call("copy-package-resource", "write_file", {
                    "path": str(self.target), "source_ref": self.source_ref,
                })
            hint = payload["next_read"]
        self.read_count += 1
        return self._call(f"archive-page-{self.read_count}", "read_artifact", hint)


def test_native_package_archive_anchor_recovers_page_then_materializes_original_bytes(tmp_path):
    raw = b"# resource\r\n" + b"original_line = True\r\n" * 800 + b"# final bytes\r\n"
    agent, _store, _entries = _agent(tmp_path, method_body=raw)
    workspace = resolve_owner_home(agent.home_paths.root).workspace_dir / "archive-resource"
    workspace.mkdir(parents=True)
    agent = SimpleAgent(replace(agent.config, tool_output_externalize_min_chars=100,
                                tool_output_preview_chars=200, tool_read_max_chars=5000), workspace)
    target = agent.root / "original-resource.py"
    backend = _PackageArchiveReadBackend(target)
    agent.backend = backend
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "archive-resource",
    })

    result = agent.run("读完现有资料，再把原文件整理到工作区。", save=False,
                       allowed_tools=["skill_search", "read_artifact", "write_file"],
                       task_attributes={"conversation_thread_id": thread.thread_id},
                       context_scope="conversation")

    assert result.response == "已读回资源页并按原资源生成文件。"
    assert backend.read_count >= 2
    assert backend.restored_page["body"] == raw.decode()[:5000]
    assert backend.restored_page["has_more"] is True
    assert target.read_bytes() == raw
