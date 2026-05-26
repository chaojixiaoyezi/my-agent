from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.tools import ToolRegistry, ToolRegistryParams


def test_memory_artifact_read_cli_reads_registered_tool_output_artifact(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    workspace = _workspace(config_path)
    artifact_path = _write_externalized_tool_output(workspace, content="alpha\n" + ("z" * 1300))
    parser = build_parser()

    args = parser.parse_args([
        "--config",
        str(config_path),
        "memory-artifact-read",
        str(artifact_path),
        "--max-chars",
        "0",
        "--json",
    ])
    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["artifact_ref"] == str(artifact_path)
    assert payload["content_hash_verified"] is True
    assert payload["reads_artifact_body"] is True
    assert payload["content"].startswith("alpha\n")
    assert payload["content"].endswith("z" * 20)
    assert payload["truncated"] is False


def test_memory_artifact_read_cli_rejects_unindexed_arbitrary_file(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    workspace = _workspace(config_path)
    unindexed = workspace / "notes.txt"
    unindexed.parent.mkdir(parents=True, exist_ok=True)
    unindexed.write_text("not an artifact", encoding="utf-8")
    parser = build_parser()

    args = parser.parse_args(["--config", str(config_path), "memory-artifact-read", str(unindexed), "--json"])
    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["ok"] is False
    assert payload["error_code"] == "artifact_not_registered"
    assert "content" not in payload


def test_read_artifact_tool_reads_explicit_slice_from_registered_artifact(tmp_path: Path) -> None:
    artifact_path = _write_externalized_tool_output(tmp_path, content="abcdef" * 300)
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=20,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=5,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
    )

    result = registry.execute_call({
        "tool": "read_artifact",
        "artifact_ref": str(artifact_path),
        "offset": 2,
        "max_chars": 5,
    })
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["ok"] is True
    assert payload["content"] == "cdefa"
    assert payload["truncated"] is True
    assert payload["content_hash_verified"] is True
    assert payload["reads_artifact_body"] is True


# LLM: artifact read modes should let agents inspect large outputs without rereading the whole body.
# 函数用途: 验证 read_artifact 支持 head/tail/search/slice 模式，方便子代理只读需要的片段或关键行。
def test_read_artifact_supports_head_tail_and_search_modes(tmp_path: Path) -> None:
    content = "alpha first\nbeta middle\nneedle here\nbeta after\nomega last"
    artifact_path = _write_externalized_tool_output(tmp_path, content=content)

    head = _read_artifact_payload(tmp_path, artifact_path, {"mode": "head", "max_chars": 11})
    tail = _read_artifact_payload(tmp_path, artifact_path, {"mode": "tail", "max_chars": 10})
    search = _read_artifact_payload(tmp_path, artifact_path, {"mode": "search", "query": "beta", "max_chars": 80})

    assert head["ok"] is True
    assert head["read_mode"] == "head"
    assert head["content"] == "alpha first"
    assert tail["ok"] is True
    assert tail["read_mode"] == "tail"
    assert tail["content"] == "omega last"
    assert search["ok"] is True
    assert search["read_mode"] == "search"
    assert search["search_query"] == "beta"
    assert search["match_count"] == 2
    assert "2: beta middle" in search["content"]
    assert "4: beta after" in search["content"]


# LLM: artifact read budgets prevent repeated artifact-body pulls from flooding prompts or disk IO.
# 函数用途: 验证同一 run 在窗口内超过 artifact 正文读取字符预算时会被阻断，兄弟 run 不受影响。
def test_read_artifact_tool_enforces_per_run_artifact_read_budget(tmp_path: Path) -> None:
    artifact_path = _write_externalized_tool_output(tmp_path, content="0123456789" * 20)
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=20,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=5,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
            artifact_read_budget_window_seconds=600,
            artifact_read_budget_max_chars=12,
        )
    )

    first = registry.execute_call({
        "tool": "read_artifact",
        "artifact_ref": str(artifact_path),
        "run_id": "reader-a",
        "max_chars": 8,
    })
    second = registry.execute_call({
        "tool": "read_artifact",
        "artifact_ref": str(artifact_path),
        "run_id": "reader-a",
        "max_chars": 8,
    })
    sibling = registry.execute_call({
        "tool": "read_artifact",
        "artifact_ref": str(artifact_path),
        "run_id": "reader-b",
        "max_chars": 8,
    })

    assert first.ok is True
    assert second.ok is False
    assert "artifact 读取预算" in second.output
    assert "reader-a" in second.output
    assert sibling.ok is True


# LLM: unbounded reads should use the artifact index size before loading the body.
# 函数用途: 验证 max_chars=0 读取全部时，会先按 index 里的 size_bytes 做预算预判，避免大 artifact 被直接展开。
def test_read_artifact_budget_blocks_unbounded_large_read_from_index(tmp_path: Path) -> None:
    artifact_path = _write_externalized_tool_output(tmp_path, content="0123456789" * 20)
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=20,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=5,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
            artifact_read_budget_window_seconds=600,
            artifact_read_budget_max_chars=12,
        )
    )

    result = registry.execute_call({
        "tool": "read_artifact",
        "artifact_ref": str(artifact_path),
        "run_id": "reader-a",
        "max_chars": 0,
    })

    assert result.ok is False
    assert "artifact 读取预算" in result.output
    assert "本次请求" in result.output


def test_read_artifact_tool_repairs_wrong_prefix_with_unique_artifact_name(tmp_path: Path) -> None:
    artifact_path = _write_externalized_tool_output(tmp_path, content="abcdef" * 300)
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=20,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=5,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
    )

    result = registry.execute_call({
        "tool": "read_artifact",
        "artifact_ref": f"/wrong/workspace/memory_archive/artifacts/tool_outputs/{artifact_path.name}",
        "offset": 0,
        "max_chars": 6,
    })
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["ok"] is True
    assert payload["artifact_path"] == str(artifact_path)
    assert payload["content"] == "abcdef"


# LLM: short call ids must resolve by current run scope before falling back to latest.
# 函数用途: 防止 read_artifact("17-1") 在长任务里读到旧 run 的同号工具输出，导致路径和 prompt 串线。
def test_read_artifact_short_call_id_prefers_matching_run_scope(tmp_path: Path) -> None:
    _write_externalized_tool_output(
        tmp_path,
        content="old-run-output",
        call_id="17-1",
        run_id="old-run",
    )
    _write_externalized_tool_output(
        tmp_path,
        content="new-run-output",
        call_id="17-1",
        run_id="new-run",
    )
    registry = _registry(tmp_path)

    result = registry.execute_call({
        "tool": "read_artifact",
        "artifact_ref": "17-1",
        "run_id": "old-run",
        "offset": 0,
        "max_chars": 0,
    })
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["ok"] is True
    assert payload["content"] == "old-run-output"
    assert payload["run_id"] == "old-run"


# LLM: unscoped short call ids should choose the newest record instead of the oldest legacy collision.
# 函数用途: 没有 run_id 注入的旧调用也不能优先读到历史测试的同号 artifact。
def test_read_artifact_short_call_id_without_scope_prefers_latest(tmp_path: Path) -> None:
    _write_externalized_tool_output(
        tmp_path,
        content="older-output",
        call_id="17-1",
        run_id="older-run",
    )
    _write_externalized_tool_output(
        tmp_path,
        content="latest-output",
        call_id="17-1",
        run_id="latest-run",
    )
    registry = _registry(tmp_path)

    result = registry.execute_call({
        "tool": "read_artifact",
        "artifact_ref": "17-1",
        "offset": 0,
        "max_chars": 0,
    })
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["content"] == "latest-output"
    assert payload["run_id"] == "latest-run"


# LLM: read_file is the normal path for explicit tool-output artifact paths.
# 函数用途: 验证模型拿到外置工具输出路径后，可以直接用 read_file 读取正文切片。
def test_read_file_reads_tool_output_artifact_content(tmp_path: Path) -> None:
    artifact_path = _write_externalized_tool_output(tmp_path, content="large-output" * 500)
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=20,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=5,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
    )

    result = registry.execute_call({"tool": "read_file", "path": str(artifact_path)})

    assert result.ok is True
    assert "1: large-output" in result.output
    assert "... 已截断" in result.output
    assert '"kind": "tool_output"' not in result.output


# LLM: read_file should not require read_artifact permission for explicit artifact wrapper paths.
# 函数用途: 即使当前上下文只授权 read_file，也能读取安全路径下 tool-output artifact 的正文。
def test_read_file_artifact_wrapper_does_not_require_read_artifact_permission(tmp_path: Path) -> None:
    artifact_path = _write_externalized_tool_output(tmp_path, content="alpha\nbeta\n" * 20)
    registry = _registry(tmp_path)

    result = registry.execute_call(
        {"tool": "read_file", "path": str(artifact_path), "start_line": 2, "end_line": 3},
        allowed_tools=["read_file"],
    )

    assert result.ok is True
    assert "2: beta" in result.output
    assert "3: alpha" in result.output


# LLM: typo recovery should stay on read_file instead of switching tools.
# 函数用途: 模型把 artifact 绝对路径前缀抄错时，提示继续 read_file suggested_target。
def test_read_file_typo_to_tool_output_artifact_keeps_read_file_recovery(tmp_path: Path) -> None:
    artifact_path = _write_externalized_tool_output(tmp_path, content="large-output" * 500)
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=20,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=5,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
    )
    wrong_prefix = f"/wrong/{tmp_path.name}/memory_archive/artifacts/tool_outputs/{artifact_path.name}"

    result = registry.execute_call({"tool": "read_file", "path": wrong_prefix})

    assert result.ok is False
    assert "suspected_path_typo=true" in result.output
    assert "read_file" in result.output
    assert artifact_path.name in result.output
    assert "suggested_target" in result.output


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def _workspace(config_path: Path) -> Path:
    return config_path.parent / "workspace"


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=1000,
            max_entries=20,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=5,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
    )


# LLM: _read_artifact_payload keeps mode tests focused on the public memory reader contract.
# 函数用途: 直接调用 artifact reader 并返回 JSON payload，避免重复构造 registry。
def _read_artifact_payload(
    root: Path,
    artifact_path: Path,
    options: dict[str, object],
) -> dict:
    from agent_py_agent.agent.memory_archive.artifact_reader import (
        ReadToolOutputArtifactRequest,
        read_tool_output_artifact,
    )

    return read_tool_output_artifact(
        ReadToolOutputArtifactRequest(
            root=root,
            artifact_ref=str(artifact_path),
            mode=str(options.get("mode") or "slice"),
            max_chars=int(options.get("max_chars") or 4000),
            query=str(options.get("query") or ""),
        )
    )


def _write_externalized_tool_output(
    root: Path,
    *,
    content: str,
    call_id: str = "call-artifact",
    run_id: str = "run-artifact",
) -> Path:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="blackbox_tool",
            call_id=call_id,
            output=content,
            ok=True,
            request_id="req-artifact",
            run_id=run_id,
            task_id="task-artifact",
            min_chars=10,
        )
    )
    return Path(record["artifact_ref"])
