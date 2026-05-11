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


# LLM: test_read_file_rejects_tool_output_artifact_wrapper captures the R16 prompt-bloat regression.
# 函数用途: 防止模型用 read_file 直接读取外置工具输出 JSON 包装，必须改走 read_artifact 分片。
def test_read_file_rejects_tool_output_artifact_wrapper(tmp_path: Path) -> None:
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

    assert result.ok is False
    assert "read_artifact" in result.output
    assert "max_chars" in result.output


# LLM: test_read_file_typo_to_tool_output_artifact_routes_to_read_artifact catches copied-prefix drift.
# 函数用途: 模型把 artifact 绝对路径前缀抄错时，read_file 也要提示改用 read_artifact，而不是按 suggested_target 继续读文件。
def test_read_file_typo_to_tool_output_artifact_routes_to_read_artifact(tmp_path: Path) -> None:
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
    assert "read_artifact" in result.output
    assert artifact_path.name in result.output
    assert "不要用 read_file" in result.output


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
