from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.capability.channel_message_tool import SendMessageTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.settings import AgentConfig


class _RecordingAdapter:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.files: list[tuple[str, Path]] = []

    def send_message(self, user_id: str, message: object) -> bool:
        self.messages.append((user_id, str(getattr(message, "content", "") or "")))
        return True

    def send_file(self, user_id: str, path: Path) -> bool:
        self.files.append((user_id, path))
        return True


def _tool(owner_root: Path) -> tuple[SendMessageTool, _RecordingAdapter]:
    agent = SimpleNamespace(
        config=SimpleNamespace(
            feishu_app_id="",
            feishu_app_secret="",
            my_agent_owner_id="ou_current_user",
        ),
        home_paths=SimpleNamespace(
            owner_provider="feishu",
            owner_id="providers/feishu/users/ou_current_user",
            owner_home_dir=owner_root,
        ),
    )
    tool = SendMessageTool(agent)
    adapter = _RecordingAdapter()
    tool._delivery.registry.register_adapter(
        "feishu",
        adapter,
        capabilities=ChannelCapabilities(text=True, reply=True, proactive=True, files=True),
    )
    return tool, adapter


def test_send_message_uses_task_registry_and_native_attachment_api(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "2026-07-13" / "weekly-report"
    artifact = task_root / "output" / "weekly.xlsx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"verified workbook")
    register_artifact(
        ArtifactRegistration(
            workspace_root=task_root,
            path=artifact,
            artifact_id="weekly_report",
            status="ready",
            source="test",
        )
    )
    tool, adapter = _tool(owner_root)
    params = {
        "message": "给你周报。",
        "attachments": [str(artifact)],
        "__run_scope": {"request_id": "gw-1", "run_id": "run-1"},
        "__tool_call_id": "call-1",
    }

    first = tool.execute(params)
    second = tool.execute(params)

    assert first.ok is True
    assert second.ok is True
    assert adapter.messages == [("ou_current_user", "给你周报。")]
    assert adapter.files == [("ou_current_user", artifact)]
    payload = json.loads(first.output)
    assert payload["delivery_status"] == "sent"
    assert payload["attachments"][0]["artifact_id"] == "weekly_report"
    assert str(owner_root) not in first.output
    assert "ou_current_user" not in first.output
    assert json.loads(second.output)["deduplicated"] is True


def test_send_message_rejects_unregistered_or_cross_owner_file(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    outside = tmp_path / "other-owner" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("secret", encoding="utf-8")
    tool, adapter = _tool(owner_root)

    result = tool.execute({"attachments": [str(outside)]})

    assert result.ok is False
    assert result.error_code == "ARTIFACT_NOT_REGISTERED"
    assert adapter.messages == []
    assert adapter.files == []


def test_send_message_rejects_file_changed_after_registration(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "2026-07-13" / "report"
    artifact = task_root / "output" / "report.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"first")
    register_artifact(
        ArtifactRegistration(
            workspace_root=task_root,
            path=artifact,
            artifact_id="report",
            status="ready",
        )
    )
    artifact.write_bytes(b"changed")
    tool, adapter = _tool(owner_root)

    result = tool.execute({"attachments": [str(artifact)]})

    assert result.ok is False
    assert result.error_code == "ARTIFACT_VALIDATION_FAILED"
    assert adapter.files == []


def test_send_message_is_registered_and_retrieved_for_plain_user_language(tmp_path: Path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )

    names = {spec.name for spec in agent.tools.specs(include_orchestration=True)}
    relevant = {spec.name for spec in agent.tools.find_relevant_specs("把刚才生成的文件发我")}

    assert "send_message" in names
    assert "send_message" in relevant
