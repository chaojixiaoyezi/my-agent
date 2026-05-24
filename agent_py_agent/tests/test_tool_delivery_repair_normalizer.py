from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.tests.tool_delivery_repair_fixtures import (
    _builder_call,
    _delivery_repair_agent,
    _evidence_repair_closeout,
    _failed_builder_closeout,
    _response_decision,
    _sheet_only_write_call,
    _structure_and_evidence_repair_closeout,
    _structured_evidence_write_call,
    _tool_loop_params,
    _workbook_delivery_contract,
    _write_closeout,
    _write_ready_source,
)


def _repair_payload() -> dict[str, object]:
    return {
        "source_refs": [{"source_id": "src-1", "uri": "https://example.com"}],
        "claims": [{"field": "地址", "source_ids": ["src-1"], "value": "https://example.com"}],
    }


def _evidence_write_call(path: str = "outputs/report/source_data.json") -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": path,
        "merge_existing": False,
        "data": _repair_payload(),
    }


def _evidence_repair_action(checkpoint_ref: str = "outputs/report/source_data.json") -> dict[str, object]:
    return {
        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
        "recommended_action": "repair_evidence_refs",
        "checkpoint_ref": checkpoint_ref,
        "required_fields": ["地址"],
        "writer_tool": "write_structured_json",
    }


def _closeout_with_actions(root: Path, actions: list[dict[str, object]], **progress: object) -> None:
    _write_closeout(root, {"ok": False, "delivery_progress": {"recovery_actions": actions, **progress}})


# LLM: evidence metadata repair writes are normalized into merge_existing before execution.
# 函数用途: 验证补证据时不会覆盖已有 checkpoint 数据。
def test_delivery_repair_normalizes_evidence_write_to_merge_existing(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_call_normalizer import (
        normalize_delivery_repair_calls,
    )

    _closeout_with_actions(tmp_path, [_evidence_repair_action(), _structured_repair_action("outputs/other/source.json")])
    calls = [_evidence_write_call(), _sheet_write_call("outputs/other/source.json")]

    normalized = normalize_delivery_repair_calls(SimpleNamespace(root=tmp_path), calls)

    assert normalized[0]["merge_existing"] is True
    assert normalized[1]["merge_existing"] is False
    assert calls[0]["merge_existing"] is False


# LLM: JSON text writes to structured checkpoints should enter the structured writer gate.
# 函数用途: 模型把 JSON checkpoint 误用 write_file 提交时，底层只按机器合同转换为 write_structured_json。
def test_delivery_repair_normalizes_json_write_file_to_structured_checkpoint_writer(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_call_normalizer import (
        normalize_delivery_repair_calls,
    )

    _closeout_with_actions(tmp_path, [_source_checkpoint_action()])
    content = json.dumps({"items": [{"title": "DeepSeek-R1", "url": "https://example.com/paper.pdf"}]})

    normalized = normalize_delivery_repair_calls(
        SimpleNamespace(root=tmp_path),
        [
            {
                "tool": "write_file",
                "path": "outputs/document_delivery/source_index.json",
                "content": content,
            }
        ],
    )

    assert normalized == [
        {
            "tool": "write_structured_json",
            "path": "outputs/document_delivery/source_index.json",
            "data": {"items": [{"title": "DeepSeek-R1", "url": "https://example.com/paper.pdf"}]},
        }
    ]


# LLM: non-JSON file writes are not rewritten as structured checkpoint writes.
# 函数用途: 归一化只接受可解析 JSON，普通文本写入仍交给后续合同门判断。
def test_delivery_repair_keeps_non_json_write_file_unchanged(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_call_normalizer import (
        normalize_delivery_repair_calls,
    )

    _closeout_with_actions(tmp_path, [_source_checkpoint_action()])
    call = {
        "tool": "write_file",
        "path": "outputs/document_delivery/source_index.json",
        "content": "not json",
    }

    assert normalize_delivery_repair_calls(SimpleNamespace(root=tmp_path), [call]) == [call]


# LLM: structured writer normalization is scoped to declared checkpoint refs.
# 函数用途: 即使 content 是 JSON，只要路径不匹配 recovery action，也不能被系统改写。
def test_delivery_repair_keeps_json_write_file_for_unrelated_path(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_call_normalizer import (
        normalize_delivery_repair_calls,
    )

    _closeout_with_actions(tmp_path, [_source_checkpoint_action()])
    call = {
        "tool": "write_file",
        "path": "outputs/other/source_index.json",
        "content": json.dumps({"items": []}),
    }

    assert normalize_delivery_repair_calls(SimpleNamespace(root=tmp_path), [call]) == [call]


# LLM: source-backed required writers should replace another inspection round.
# 函数用途: 已有 source_artifacts 时，read_artifact/list_files 不能继续空转，应执行合同声明的结构化 writer。
def test_delivery_repair_replaces_inspection_with_source_artifact_required_writer(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_call_normalizer import (
        normalize_delivery_repair_calls,
    )

    artifact_path = _write_tool_output_artifact(tmp_path)
    _closeout_with_actions(tmp_path, [_source_checkpoint_action()])

    normalized = normalize_delivery_repair_calls(
        SimpleNamespace(root=tmp_path),
        [{"tool": "read_artifact", "artifact_ref": str(artifact_path), "max_chars": 8000}],
    )

    assert normalized[0]["tool"] == "api_json_collection"
    assert normalized[0]["path"] == "outputs/document_delivery/source_index.json"
    assert normalized[0]["source_artifacts"][0]["artifact_ref"] == str(artifact_path)
    assert normalized[0]["item_path"] == "results"


# LLM: Directory setup commands should not stall source-backed checkpoint repair.
# 函数用途: 当来源 artifact 已存在且合同已有可执行 writer 时，mkdir 这类准备动作会被归一成真正的 checkpoint 写入。
def test_delivery_repair_replaces_directory_setup_with_source_artifact_required_writer(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_call_normalizer import (
        normalize_delivery_repair_calls,
    )

    artifact_path = _write_tool_output_artifact(tmp_path)
    _closeout_with_actions(tmp_path, [_source_checkpoint_action()])

    normalized = normalize_delivery_repair_calls(
        SimpleNamespace(root=tmp_path),
        [{"tool": "run_command", "command": "mkdir -p outputs/document_delivery"}],
    )

    assert normalized[0]["tool"] == "api_json_collection"
    assert normalized[0]["path"] == "outputs/document_delivery/source_index.json"
    assert normalized[0]["source_artifacts"][0]["artifact_ref"] == str(artifact_path)


# LLM: materialize_checkpoint cannot treat arbitrary artifact reads as progress.
# 函数用途: checkpoint 修复只能读取合同声明的来源 artifact；随便读旧工具产物仍要被拉回写入/构建主线。
def test_delivery_repair_guard_rejects_unbound_read_artifact_for_checkpoint_materialization(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _closeout_with_actions(tmp_path, [_source_checkpoint_action()])
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/unrelated.json"}],
        )
        is False
    )


def _write_tool_output_artifact(root: Path) -> Path:
    artifact_dir = root / "memory_archive/artifacts/tool_outputs"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "web_search-1.json"
    artifact_path.write_text(
        json.dumps(
            {
                "content": json.dumps(
                    {"results": [{"snippet": "摘要", "title": "Paper", "url": "https://example.com/paper"}]},
                    ensure_ascii=False,
                ),
                "ok": True,
                "tool": "web_search",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_dir / "index.jsonl").write_text(
        json.dumps(
            {
                "call_id": "1-1",
                "path": str(artifact_path),
                "scoped_call_id": "run-1:1-1",
                "sha256": "abc",
                "tool": "web_search",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact_path


def _source_checkpoint_action() -> dict[str, object]:
    return {
        "code": "STAGING_CHECKPOINT_MISSING",
        "recommended_action": "materialize_checkpoint",
        "checkpoint_materialization_mode": "source_evidence_first",
        "checkpoint_ref": "outputs/document_delivery/source_index.json",
        "collection_contract": {
            "required_item_evidence_fields": ["title", "url"],
            "required_item_fields": ["title", "url", "translated"],
            "required_item_values": {"translated": True},
        },
        "required_columns": ["title", "url", "translated"],
        "writer_tool": "api_json_collection",
        "write_tools": ["api_json_collection", "write_structured_json"],
    }


def _structured_repair_action(checkpoint_ref: str) -> dict[str, object]:
    return {
        "code": "STAGED_JSON_TOO_FEW_SHEETS",
        "recommended_action": "repair_structured_checkpoint_json",
        "checkpoint_ref": checkpoint_ref,
        "writer_tool": "write_structured_json",
    }


def _sheet_write_call(path: str) -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": path,
        "merge_existing": False,
        "data": {"sheets": [{"name": "Sheet1", "rows": [{"记录名": "demo"}]}]},
    }


# LLM: strict no-rows repair requires non-empty table rows.
# 函数用途: 防止空 sheets 骨架被误判为有效修复。
def test_delivery_repair_guard_rejects_empty_structured_rows_for_no_rows_action(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _closeout_with_actions(tmp_path, [_no_rows_action()])
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [_empty_sheet_call()]) is False
    assert is_delivery_repair_productive_call(agent, [_non_empty_sheet_call()]) is True


def _no_rows_action() -> dict[str, object]:
    return {
        "code": "STAGED_JSON_NO_ROWS",
        "recommended_action": "write_non_empty_structured_rows",
        "checkpoint_ref": "outputs/report/source_data.json",
        "writer_tool": "write_structured_json",
    }


def _empty_sheet_call() -> dict[str, object]:
    return {"tool": "write_structured_json", "path": "outputs/report/source_data.json", "sheets": [{"name": "W01", "rows": []}]}


def _non_empty_sheet_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/report/source_data.json",
        "merge_existing": True,
        "sheets": [{"name": "W01", "rows": [{"记录名": "demo"}]}],
    }


# LLM: evidence repairs require source_refs/claims, not arbitrary rows.
# 函数用途: 验证 repair_evidence_refs 必须提交可审计机器证据。
def test_delivery_repair_guard_requires_evidence_shape_for_evidence_repair(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(tmp_path, _evidence_repair_closeout())
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [_sheet_only_write_call()]) is False
    assert is_delivery_repair_productive_call(agent, [_structured_evidence_write_call()]) is True


# LLM: structure and evidence repair must update one checkpoint with both data and refs.
# 函数用途: 防止同一个 checkpoint 只修表格结构却漏掉证据。
def test_delivery_repair_guard_requires_evidence_shape_for_combined_checkpoint_repair(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(tmp_path, _structure_and_evidence_repair_closeout())
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [_sheet_only_write_call()]) is False
    assert is_delivery_repair_productive_call(agent, [_structured_evidence_write_call()]) is True


# LLM: evidence gathering is still productive before strict repair mode trips.
# 函数用途: 前几轮允许抓取证据，但不把 list_files 当推进。
def test_delivery_repair_guard_allows_evidence_gathering_before_strict_write_mode(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _closeout_with_actions(tmp_path, [_no_rows_action()], unchanged_failure_count=1, no_progress_block_threshold=5)
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "fetch_url", "url": "https://example.com/data"}]) is True
    assert is_delivery_repair_productive_call(agent, [{"tool": "search", "query": "release notes"}]) is True
    assert is_delivery_repair_productive_call(agent, [{"tool": "list_files", "path": "outputs/report"}]) is False


# LLM: delivery repair context exposes executable checkpoint requirements.
# 函数用途: repair prompt 上下文只承载结构化 required_tool_calls 和 shape hint。
def test_delivery_repair_context_includes_checkpoint_shape_hint(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import delivery_repair_context

    _closeout_with_actions(tmp_path, [_columns_action()])

    context = delivery_repair_context(SimpleNamespace(root=tmp_path), repairs=0)

    assert "checkpoint_shape_hint" in context
    assert "required_columns" in context
    assert '"tool": "write_structured_json"' in context
    assert '"path": "outputs/report/source_data.json"' in context


def _columns_action() -> dict[str, object]:
    return {
        "code": "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
        "recommended_action": "write_non_empty_structured_rows",
        "checkpoint_ref": "outputs/report/source_data.json",
        "checkpoint_shape_hint": '{"sheets":[{"name":"榜单","rows":[{"记录名":"..."}]}]}',
        "required_columns": ["记录名", "地址"],
        "missing_columns": "记录名,地址",
        "writer_tool": "write_structured_json",
    }


# LLM: rejected repair calls are returned with machine-readable required calls.
# 函数用途: 被拦截的检查类调用不会静默消失。
def test_delivery_repair_rejection_context_names_rejected_and_required_calls(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_rejection_context,
    )

    _closeout_with_actions(tmp_path, [_missing_checkpoint_action()])
    context = delivery_repair_rejection_context(
        SimpleNamespace(root=tmp_path, backend=SimpleNamespace(name="fake")),
        [{"tool": "list_files", "path": "outputs/report"}],
        repairs=0,
    )
    payload = json.loads(context.splitlines()[1])

    assert payload["rejected_tool_calls"] == [{"path": "outputs/report", "tool": "list_files"}]
    assert payload["required_tool_calls"][0]["tool"] == "write_structured_json"


def _missing_checkpoint_action() -> dict[str, object]:
    return {
        "code": "STAGING_CHECKPOINT_MISSING",
        "recommended_action": "materialize_checkpoint",
        "checkpoint_ref": "outputs/report/source_data.json",
        "checkpoint_shape_hint": '{"sheets":[{"name":"榜单","rows":[{"记录名":"..."}]}]}',
        "writer_tool": "write_structured_json",
    }


# LLM: delivery repair payload refreshes stale closeout actions from the active contract.
# 函数用途: workbook 验收失败且 source ready 时，生成 builder required call。
def test_delivery_repair_context_refreshes_failed_builder_output_actions(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import delivery_repair_context

    _write_ready_source(tmp_path)
    _failed_builder_closeout(tmp_path)

    context = delivery_repair_context(SimpleNamespace(root=tmp_path), repairs=0, runtime_params=_tool_loop_params())
    payload = json.loads(context.splitlines()[1])

    assert any(action.get("code") == "STAGING_BUILDER_READY" for action in payload["required_actions"])
    assert _builder_call() in payload["required_tool_calls"]


# LLM: strict structured repair may inspect the exact checkpoint it will rewrite.
# 函数用途: 只放行 checkpoint_ref 本身的 read_file。
def test_delivery_repair_guard_allows_checkpoint_read_for_structured_repair(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _closeout_with_actions(tmp_path, [_structured_repair_action("outputs/report/source_data.json")], unchanged_failure_count=5, no_progress_block_threshold=5)
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/report/source_data.json"}]) is True
    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/index.html"}]) is False


# LLM: strict evidence repair still allows source gathering tools.
# 函数用途: 证据缺失时 fetch/search 仍能推进，但最终通过仍看 source_refs/claims。
def test_delivery_repair_guard_allows_evidence_gathering_for_evidence_repair(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _closeout_with_actions(tmp_path, [_evidence_repair_action()], unchanged_failure_count=5, no_progress_block_threshold=5)
    assert is_delivery_repair_productive_call(SimpleNamespace(root=tmp_path), [{"tool": "fetch_url", "url": "https://example.com"}]) is True


# LLM: delivery repair allowed calls bypass terminal local-progress blocking.
# 函数用途: 恢复动作仍在时，fetch_url 会进入执行，而不是被 no-progress guard 抢先终止。
def test_delivery_repair_productive_call_bypasses_local_progress_guard(tmp_path: Path) -> None:
    _closeout_with_actions(tmp_path, [_evidence_repair_action()], **_blocked_progress())
    _write_local_progress_state(tmp_path)

    agent = _delivery_repair_agent(tmp_path, [{"tool": "fetch_url", "url": "https://example.com/source"}])
    decision = _response_decision(agent, _tool_loop_params(), text="CALL_FETCH")

    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "fetch_url", "url": "https://example.com/source"}]


def _blocked_progress() -> dict[str, object]:
    return {
        "failure_fingerprint": "failure-1",
        "no_progress_block_threshold": 2,
        "unchanged_failure_count": 5,
        "work_progress_fingerprint": "work-1",
        "pending_materialization_targets": [{"workspace_relative_path": "outputs/report/report.xlsx", "exists": False}],
    }


def _write_local_progress_state(root: Path) -> None:
    state_path = root / ".agent_delivery" / "local_progress_guard.json"
    state_path.write_text(
        json.dumps(
            {
                "failure_fingerprint": "failure-1",
                "exploration_rounds_without_local_progress": 1,
                "recovery_signature": "",
                "work_progress_fingerprint": "work-1",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# LLM: tool-loop decisions pass normalized repair calls to the executor.
# 函数用途: merge_existing 不是提示词软约束，而是执行前的底层归一化结果。
def test_delivery_repair_response_decision_returns_normalized_evidence_write(tmp_path: Path) -> None:
    _closeout_with_actions(tmp_path, [_evidence_repair_action()])
    write_call = _evidence_write_call()

    decision = _response_decision(_delivery_repair_agent(tmp_path, [write_call]), _tool_loop_params(), text="CALL_WRITE")

    assert decision.action == "run_tools"
    assert decision.calls[0]["merge_existing"] is True
    assert write_call["merge_existing"] is False


# LLM: deterministic builder repairs execute when model keeps inspecting ready inputs.
# 函数用途: builder 就绪后，检查类调用会被归一化成合同声明的 builder 调用。
def test_delivery_repair_response_decision_replaces_inspection_with_required_builder(tmp_path: Path) -> None:
    _write_ready_source(tmp_path)
    _failed_builder_closeout(tmp_path)
    agent = _delivery_repair_agent(tmp_path, [{"tool": "read_file", "path": "outputs/report/source_data.json"}])

    decision = _response_decision(agent, _tool_loop_params(_workbook_delivery_contract()), text="CALL_READ")

    assert decision.action == "run_tools"
    assert decision.calls == [_builder_call()]


# LLM: mapping repairs must materialize the source-backed markdown before running builders.
# 函数用途: 当集合映射和 builder 同时待修时，检查类调用先被归一成 write_file，而不是过早生成 PDF。
def test_delivery_repair_normalizer_prioritizes_mapping_writer_before_builder(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_call_normalizer import (
        normalize_delivery_repair_calls,
    )

    _write_mapping_source_index(tmp_path)
    _closeout_with_actions(tmp_path, [_mapping_writer_action(), _pdf_builder_action()])

    normalized = normalize_delivery_repair_calls(
        SimpleNamespace(root=tmp_path),
        [{"tool": "web_search", "query": "more sources"}],
    )

    assert normalized[0]["tool"] == "write_file"
    assert normalized[0]["path"] == "outputs/research/research.md"
    assert "Paper A" in normalized[0]["content"]
    assert "https://example.com/a" in normalized[0]["content"]


def _write_mapping_source_index(root: Path) -> None:
    path = root / "outputs/research/source_index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "rows": [
                    {"title": "Paper A", "url": "https://example.com/a", "date": "2025-01-01", "translated": True},
                    {"title": "Paper B", "url": "https://example.com/b", "date": "2025-02-01", "translated": True},
                    {"title": "Paper C", "url": "https://example.com/c", "date": "2025-03-01", "translated": True},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _mapping_writer_action() -> dict[str, object]:
    return {
        "artifact_path": "outputs/research/research.md",
        "checkpoint_ref": "outputs/research/source_index.json",
        "code": "ARTIFACT_MAPPING_MISSING",
        "finding_codes": ["ARTIFACT_MAPPING_MISSING"],
        "finding_values": [
            json.dumps(
                {
                    "mapped": 0,
                    "missing_keys": [{"title": "Paper A"}, {"title": "Paper B"}, {"title": "Paper C"}],
                    "required": 3,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ],
        "recommended_action": "repair_artifact_against_findings",
        "repair_targets": ["outputs/research/research.md"],
        "source_ref": "outputs/research/source_index.json",
        "write_tools": ["write_file", "replace_in_file"],
    }


def _pdf_builder_action() -> dict[str, object]:
    return {
        "builder_tool": "markdown_to_pdf",
        "code": "STAGING_BUILDER_READY",
        "output_ref": "outputs/research/research.pdf",
        "recommended_action": "invoke_builder_tool",
        "source_ref": "outputs/research/research.md",
    }
