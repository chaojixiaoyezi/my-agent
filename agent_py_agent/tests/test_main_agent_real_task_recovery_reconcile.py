"""Tests for real-task recovery reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.contracts.main_agent_real_task_recovery_packet import SCHEMA_VERSION


# LLM: recovery reconciliation should resolve duplicate open write sessions without asking the model to copy IDs.
# 函数用途: 验证同一目标文件存在多个 open session 时，系统续跑前会保留进度最多的 session 并 abort 空重复 session。
def test_reconcile_recovery_open_write_sessions_aborts_duplicate_empty_session(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_recovery_reconcile import (
        reconcile_recovery_open_write_sessions,
    )

    workspace = tmp_path / "real-e2e"
    task_workspace = workspace / "task-workspace"
    empty_manifest = _write_manifest(task_workspace, "empty-session", "outputs/report.py", {})
    kept_manifest = _write_manifest(task_workspace, "kept-session", "outputs/report.py", {"0": {}})
    packet = _write_packet(workspace, task_workspace, [empty_manifest, kept_manifest])

    summary = reconcile_recovery_open_write_sessions(packet, workspace=workspace)

    assert summary["groups"][0]["kept_session_id"] == "kept-session"
    assert summary["groups"][0]["aborted_session_ids"] == ["empty-session"]
    assert _read_json(empty_manifest)["status"] == "aborted"
    assert _read_json(kept_manifest)["status"] == "open"


# LLM: Reconciliation must include current workspace manifests, not only stale recovery packet facts.
# 函数用途: 验证续跑前会扫描任务工作区，把恢复包之后产生的重复 session 也纳入去重。
def test_reconcile_recovery_open_write_sessions_scans_current_workspace(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_recovery_reconcile import (
        reconcile_recovery_open_write_sessions,
    )

    workspace = tmp_path / "real-e2e"
    task_workspace = workspace / "task-workspace"
    stale_manifest = _write_manifest(task_workspace, "stale-session", "outputs/report.py", {"0": {}})
    newer_manifest = _write_manifest(
        task_workspace,
        "newer-session",
        "outputs/report.py",
        {"0": {}, "1": {}, "2": {}},
    )
    packet = _write_packet(workspace, task_workspace, [stale_manifest])

    summary = reconcile_recovery_open_write_sessions(packet, workspace=workspace)

    assert summary["groups"][0]["kept_session_id"] == "newer-session"
    assert summary["groups"][0]["aborted_session_ids"] == ["stale-session"]
    assert summary["groups"][0]["kept_next_chunk_index"] == 3
    assert summary["groups"][0]["kept_received_chunks"] == [0, 1, 2]
    assert _read_json(stale_manifest)["status"] == "aborted"
    assert _read_json(newer_manifest)["status"] == "open"


# LLM: Stale packets must not revive already-retired write sessions.
# 函数用途: 验证恢复包指向旧 open session、但磁盘已把它标为 aborted 时，系统仍返回结构化 retired 列表并推荐当前 open session。
def test_reconcile_recovery_open_write_sessions_reports_already_aborted_stale_packet(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_recovery_reconcile import (
        reconcile_recovery_open_write_sessions,
    )

    workspace = tmp_path / "real-e2e"
    task_workspace = workspace / "task-workspace"
    stale_manifest = _write_manifest(task_workspace, "stale-session", "outputs/report.py", {"0": {}})
    current_manifest = _write_manifest(task_workspace, "current-session", "outputs/report.py", {"0": {}, "1": {}})
    packet = _write_packet(workspace, task_workspace, [stale_manifest])
    _set_manifest_status(stale_manifest, "aborted")

    summary = reconcile_recovery_open_write_sessions(packet, workspace=workspace)

    assert summary["groups"][0]["kept_session_id"] == "current-session"
    assert summary["groups"][0]["retired_session_ids"] == ["stale-session"]
    assert summary["groups"][0]["already_closed_session_ids"] == ["stale-session"]
    assert summary["groups"][0]["aborted_session_ids"] == []
    assert _read_json(stale_manifest)["status"] == "aborted"
    assert _read_json(current_manifest)["status"] == "open"


# LLM: Bad runtime_finding entries must not erase valid recovery facts from the same packet.
# 函数用途: 验证 runtime_findings 混入脏项时只忽略脏项，仍保留可用 OPEN_FILE_WRITE_SESSION finding。
def test_reconcile_recovery_open_write_sessions_skips_bad_runtime_finding_items(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_recovery_reconcile import (
        reconcile_recovery_open_write_sessions as reconcile_real,
    )
    from agent_py_agent.agent.contracts.main_agent_task_recovery_packet import (
        SCHEMA_VERSION as TASK_SCHEMA_VERSION,
    )
    from agent_py_agent.agent.contracts.main_agent_task_recovery_reconcile import (
        reconcile_recovery_open_write_sessions as reconcile_task,
    )

    real_workspace = tmp_path / "real-e2e"
    task_workspace = tmp_path / "task-e2e"

    real_summary = reconcile_real(
        _write_dirty_runtime_findings_packet(real_workspace, SCHEMA_VERSION),
        workspace=real_workspace,
    )
    task_summary = reconcile_task(
        _write_dirty_runtime_findings_packet(task_workspace, TASK_SCHEMA_VERSION),
        workspace=task_workspace,
    )

    assert real_summary["groups"][0]["kept_session_id"] == "kept-session"
    assert real_summary["groups"][0]["retired_session_ids"] == ["old-session"]
    assert task_summary["groups"][0]["kept_session_id"] == "kept-session"
    assert task_summary["groups"][0]["retired_session_ids"] == ["old-session"]


# LLM: Invalid recovery packet JSON should become a structured recovery diagnostic.
# 函数用途: 验证坏 recovery_packet 不会用 JSONDecodeError 打断 reconcile，而是返回稳定 finding code。
def test_reconcile_recovery_open_write_sessions_reports_invalid_packet_json(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_recovery_reconcile import (
        reconcile_recovery_open_write_sessions as reconcile_real,
    )
    from agent_py_agent.agent.contracts.main_agent_task_recovery_reconcile import (
        reconcile_recovery_open_write_sessions as reconcile_task,
    )

    real_workspace = tmp_path / "real-e2e"
    task_workspace = tmp_path / "task-e2e"
    real_packet = real_workspace / "recovery_packet.json"
    task_packet = task_workspace / "recovery_packet.json"
    real_packet.parent.mkdir(parents=True)
    task_packet.parent.mkdir(parents=True)
    real_packet.write_text("{bad json", encoding="utf-8")
    task_packet.write_text("{bad json", encoding="utf-8")

    real_summary = reconcile_real(real_packet, workspace=real_workspace)
    task_summary = reconcile_task(task_packet, workspace=task_workspace)

    assert real_summary["status"] == "invalid_recovery_packet"
    assert real_summary["findings"][0]["code"] == "RECOVERY_PACKET_INVALID_JSON"
    assert task_summary["status"] == "invalid_recovery_packet"
    assert task_summary["findings"][0]["code"] == "RECOVERY_PACKET_INVALID_JSON"


# LLM: One bad manifest ref must not kill reconciliation for the rest of the group.
# 函数用途: 验证越界 manifest_path 只生成 invalid_manifest_ref，不影响保留正常 session。
def test_reconcile_recovery_open_write_sessions_isolates_outside_manifest_refs(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_recovery_reconcile import (
        reconcile_recovery_open_write_sessions as reconcile_real,
    )
    from agent_py_agent.agent.contracts.main_agent_task_recovery_packet import (
        SCHEMA_VERSION as TASK_SCHEMA_VERSION,
    )
    from agent_py_agent.agent.contracts.main_agent_task_recovery_reconcile import (
        reconcile_recovery_open_write_sessions as reconcile_task,
    )

    real_workspace = tmp_path / "real-e2e"
    task_workspace = tmp_path / "task-e2e"

    real_summary = reconcile_real(
        _write_outside_manifest_packet(real_workspace, SCHEMA_VERSION),
        workspace=real_workspace,
    )
    task_summary = reconcile_task(
        _write_outside_manifest_packet(task_workspace, TASK_SCHEMA_VERSION),
        workspace=task_workspace,
    )

    assert real_summary["groups"][0]["kept_session_id"] == "kept-session"
    assert real_summary["groups"][0]["invalid_manifest_ref_session_ids"] == ["bad-session"]
    assert task_summary["groups"][0]["kept_session_id"] == "kept-session"
    assert task_summary["groups"][0]["invalid_manifest_ref_session_ids"] == ["bad-session"]


# LLM: _write_manifest creates the minimal file_write_session manifest used by reconciliation tests.
# 函数用途: 在测试工作区写一个 open manifest，模拟真实超时后遗留的分块写入会话。
def _write_manifest(
    task_workspace: Path, session_id: str, target_path: str, chunks: dict[str, object]
) -> Path:
    path = task_workspace / ".agent_file_write_sessions" / session_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "status": "open",
                "target_path": {"display": target_path, "raw": target_path},
                "chunks": chunks,
            }
        ),
        encoding="utf-8",
    )
    return path


# LLM: _write_packet stores a compact recovery packet with structured runtime findings.
# 函数用途: 构造包含 task_workspace_ref 和 OPEN_FILE_WRITE_SESSION findings 的恢复包。
def _write_packet(workspace: Path, task_workspace: Path, manifests: list[Path]) -> Path:
    packet = workspace / "recovery_packet.json"
    packet.parent.mkdir(parents=True, exist_ok=True)
    packet.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "case_id": "case",
                "refs": {"task_workspace_ref": str(task_workspace.relative_to(workspace))},
                "acceptance": {"runtime_findings": [_finding(path) for path in manifests]},
            }
        ),
        encoding="utf-8",
    )
    return packet


def _write_dirty_runtime_findings_packet(workspace: Path, schema_version: str) -> Path:
    task_workspace = workspace / "task-workspace"
    packet = workspace / "dirty_recovery_packet.json"
    packet.parent.mkdir(parents=True, exist_ok=True)
    packet.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "case_id": "case",
                "refs": {"task_workspace_ref": str(task_workspace.relative_to(workspace))},
                "acceptance": {
                    "runtime_findings": [
                        "bad-finding-entry",
                        _runtime_finding("old-session", "outputs/report.py", 1),
                        _runtime_finding("kept-session", "outputs/report.py", 3),
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return packet


def _write_outside_manifest_packet(workspace: Path, schema_version: str) -> Path:
    task_workspace = workspace / "task-workspace"
    packet = workspace / "outside_manifest_recovery_packet.json"
    kept_manifest = _write_manifest(task_workspace, "kept-session", "outputs/report.py", {"0": {}, "1": {}})
    packet.parent.mkdir(parents=True, exist_ok=True)
    packet.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "case_id": "case",
                "refs": {"task_workspace_ref": str(task_workspace.relative_to(workspace))},
                "acceptance": {
                    "runtime_findings": [
                        _finding(kept_manifest),
                        {
                            "code": "OPEN_FILE_WRITE_SESSION",
                            "session_id": "bad-session",
                            "manifest_path": "../outside/manifest.json",
                            "target_path": {"display": "outputs/report.py"},
                            "received_chunks": [],
                            "next_chunk_index": 0,
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return packet


def _runtime_finding(session_id: str, target: str, next_chunk: int) -> dict[str, object]:
    return {
        "code": "OPEN_FILE_WRITE_SESSION",
        "manifest_path": f".agent_file_write_sessions/{session_id}/manifest.json",
        "next_chunk_index": next_chunk,
        "received_chunks": list(range(next_chunk)),
        "session_id": session_id,
        "target_path": {"display": target},
    }


# LLM: _finding derives one runtime finding from the manifest JSON object.
# 函数用途: 把 manifest 转成 OPEN_FILE_WRITE_SESSION finding，保持测试输入与验收报告结构一致。
def _finding(path: Path) -> dict[str, object]:
    manifest = _read_json(path)
    chunks = sorted(int(index) for index in manifest.get("chunks", {}))
    return {
        "code": "OPEN_FILE_WRITE_SESSION",
        "session_id": manifest["session_id"],
        "manifest_path": str(path),
        "target_path": manifest["target_path"],
        "received_chunks": chunks,
        "next_chunk_index": (max(chunks) + 1) if chunks else 0,
    }


# LLM: _read_json keeps test assertions small.
# 函数用途: 读取 JSON 文件为 dict，测试只关注状态字段。
def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


# LLM: _set_manifest_status mutates test fixtures after packet creation to simulate stale packets.
# 函数用途: 让 recovery_packet 仍保留旧 open finding，但磁盘 manifest 已变成 aborted/finished。
def _set_manifest_status(path: Path, status: str) -> None:
    payload = _read_json(path)
    payload["status"] = status
    path.write_text(json.dumps(payload), encoding="utf-8")
