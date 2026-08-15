"""Stage-2 静默吞异常、decode 崩溃与非原子写测试。

覆盖:
- H6: SSE 流坏字节(非 UTF-8 切片)不崩,errors="replace" 兜底。
- H7: post_json 收到非 JSON / 坏字节响应体 → 归一为可恢复 ProviderResponseError,不裸崩。
- M1: 关键吞异常处(capability 裁决账本 save 失败 / session 心跳 save 失败)至少有日志。
- M5: runner 结果与 context bundle 原子写(temp+replace)。
"""
from __future__ import annotations

import json
import logging
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.backends.errors import (
    ProviderResponseError,
    is_provider_recoverable_error,
)
from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest


class IterableBytesIO(BytesIO):
    """BytesIO that iterates over physical lines (mimics urllib response)."""

    def __iter__(self):
        return iter(self.readline, b"")


def _request(api_key: str = "test-key", payload: dict | None = None) -> GatewayRequest:
    return GatewayRequest(
        api_base="https://api.example.com",
        api_key=api_key,
        path="/v1/chat",
        payload=payload or {},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )


# ── H6: SSE 流坏字节不崩(errors="replace") ──────────────────────────────────


class TestSseDecodeResilience:
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_stream_does_not_crash_on_bad_utf8_bytes(self, mock_urlopen):
        """SSE 行里有坏字节(被截断的多字节序列)不应抛 UnicodeDecodeError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_stream

        # 0xff 是非法 UTF-8 起始字节;\xe4\xbd 是被截断的 '你' 前缀。
        lines = [
            b'data: {"content": "ok"}',
            b"data: \xff\xfe broken",
            b"data: \xe4\xbd",
            b"data: [DONE]",
        ]
        resp = IterableBytesIO(b"\n".join(lines) + b"\n")
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        # 不崩即过;还应能拿到正常的第一行 data。
        result = post_stream(_request())
        assert any('"content": "ok"' in line for line in result)


# ── H7: post_json 非 JSON / 坏字节响应体 → 可恢复 ProviderResponseError ────────


class TestPostJsonDecodeResilience:
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_non_json_body_raises_recoverable_provider_error(self, mock_urlopen):
        """响应体不是 JSON(如 HTML 网关错误页)→ ProviderResponseError,不裸 JSONDecodeError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        resp = MagicMock()
        resp.read.return_value = b"<html><body>502 Bad Gateway</body></html>"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        with pytest.raises(ProviderResponseError) as excinfo:
            post_json(_request())
        assert is_provider_recoverable_error(excinfo.value)
        assert excinfo.value.error_code == "MODEL_RESPONSE_NOT_DECODABLE"

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_bad_utf8_body_does_not_crash_raw(self, mock_urlopen):
        """响应体有坏字节 → 归一为 ProviderResponseError(decode replace 后再 loads 失败)。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        resp = MagicMock()
        resp.read.return_value = b"\xff\xfe\x00 not json"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        with pytest.raises(ProviderResponseError):
            post_json(_request())

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_valid_json_still_parses(self, mock_urlopen):
        """回归:合法 JSON 仍正常解析。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        resp = MagicMock()
        resp.read.return_value = json.dumps({"content": "hi"}).encode("utf-8")
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        assert post_json(_request()) == {"content": "hi"}


# ── M1: 关键吞异常处至少有日志 ───────────────────────────────────────────────


class TestSilentSwallowNowLogs:
    def test_session_metadata_save_failure_logs(self, caplog):
        """session 心跳 save 失败不再无声:记 warning 日志(续跑断链排障入口)。"""
        from agent_py_agent.agent.agent_core.runner import session_pool

        manager = MagicMock()
        manager.load.side_effect = RuntimeError("store down")
        lease = SimpleNamespace(manager=manager, run_id="run-xyz", worker_id="w", interval_seconds=5.0)
        with caplog.at_level(logging.WARNING):
            session_pool._record_runner_session(lease, {"session_id": "s1"}, status="running")
        assert any("session metadata save failed" in r.message for r in caplog.records)
        assert any("run-xyz" in str(r.message) or "run-xyz" in str(r.args) for r in caplog.records)

    def test_capability_resolution_ledger_save_failure_logs(self, caplog):
        """capability 裁决账本 save 失败不再无声:记 error 日志(授权决策权威记录)。"""
        from agent_py_agent.agent.agent_core.orchestration.tools import capability

        task = SimpleNamespace(id="run-cap", attributes={})
        agent = SimpleNamespace(subagents=MagicMock())
        agent.subagents.save.side_effect = RuntimeError("save boom")
        ctx = SimpleNamespace(task=task, decision="grant", reason="")
        with caplog.at_level(logging.ERROR):
            capability._record_resolution_wake(agent, ctx, status="raised", wake_signal_id="ws-1")
        assert any("ledger save failed" in r.message for r in caplog.records)

    def test_grant_wake_failure_logs(self, caplog):
        """grant-wake 失败不再"无处查":原始错误被记日志。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_followup

        agent = SimpleNamespace(subagents=MagicMock())
        agent.subagents.load.side_effect = RuntimeError("load boom")
        with caplog.at_level(logging.WARNING):
            capability_followup._record_grant_wake_error(agent, "run-gw", RuntimeError("wake failed"))
        assert any("grant-wake failed" in r.message for r in caplog.records)

    def test_clear_pending_work_failure_logs(self, caplog):
        """_clear_pending_work 失败不再无声:记 warning。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch import loop

        class _Locked:
            __slots__ = ()  # 赋值 _has_pending_work 会抛 AttributeError

        with caplog.at_level(logging.WARNING):
            loop._clear_pending_work(_Locked())
        assert any("_clear_pending_work failed" in r.message for r in caplog.records)

    def test_is_known_subagent_run_list_failure_logs(self, caplog):
        """collaboration list_runs 故障回退 False 但记日志(不把系统不可用伪装成无匹配)。"""
        from agent_py_agent.agent.collaboration import tools as collab_tools

        manager = MagicMock()
        manager.list_runs.side_effect = RuntimeError("list down")
        agent = SimpleNamespace(subagents=manager)
        with caplog.at_level(logging.WARNING):
            result = collab_tools._is_known_subagent_run(agent, "run-1")
        assert result is False
        assert any("list_runs failed" in r.message for r in caplog.records)


# ── M5: runner 结果 / context bundle 原子写 ──────────────────────────────────


class TestAtomicWrites:
    def test_runner_result_files_written_atomically(self, tmp_path, monkeypatch):
        """_write_runner_result_files 走原子原语:写入期间不留半截 .tmp 残留。"""
        from agent_py_agent.agent.subagents import result_processors

        calls: list[str] = []
        real_text = result_processors.write_text_file_atomic
        real_json = result_processors.write_json_file_atomic

        def _spy_text(path, content):
            calls.append(("text", str(path)))
            return real_text(path, content)

        def _spy_json(path, payload, *, sort_keys=True):
            calls.append(("json", str(path)))
            return real_json(path, payload, sort_keys=sort_keys)

        monkeypatch.setattr(result_processors, "write_text_file_atomic", _spy_text)
        monkeypatch.setattr(result_processors, "write_json_file_atomic", _spy_json)

        task = MagicMock()
        task.runner_prompt_file = str(tmp_path / "prompt.txt")
        task.runner_response_file = str(tmp_path / "response.txt")
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "result.json")

        result = MagicMock()
        # asdict() 需要真实 dataclass;用最小 dataclass 替身。
        from dataclasses import dataclass

        @dataclass
        class _R:
            ok: bool = True

        result_processors._write_runner_result_files(
            task, _R(), {"run_id": "r", "ok": True}, prompt="P", response="Q"
        )

        # 全部通过原子原语;无 .tmp 残留;JSON 可正常读回。
        assert ("json", str(tmp_path / "output.json")) in calls
        assert ("json", str(tmp_path / "result.json")) in calls
        assert ("text", str(tmp_path / "prompt.txt")) in calls
        assert not list(tmp_path.glob("*.tmp"))
        assert json.loads(Path(task.output_json).read_text())["run_id"] == "r"

    def test_context_bundle_files_written_atomically(self, tmp_path):
        """write_context_bundle_files 走原子写,无 .tmp 残留,JSON 可读回。"""
        from agent_py_agent.agent.subagents import runner_context_bundle_files as rcb

        json_path = tmp_path / "ctx" / "context_bundle.json"
        md_path = tmp_path / "ctx" / "CONTEXT_BUNDLE.md"
        bundle_payload = {
            "schema_version": "context_bundle.v1",
            "run_id": "r1",
            "root_id": "r1",
            "parent_id": "",
            "depth": 0,
            "role": "runner",
            "agent_name": "a",
            "goal": "g",
            "thought": "t",
            "workspace_refs": {},
            "gate": {"ok": True, "missing_fields": [], "blocking_reason": ""},
        }
        context = SimpleNamespace(
            context_bundle=bundle_payload,
            context_bundle_json=str(json_path),
            context_bundle_file=str(md_path),
        )
        with patch.object(rcb, "render_context_bundle_markdown", return_value="# bundle"):
            rcb.write_context_bundle_files(context)

        assert json_path.exists()
        assert md_path.exists()
        assert md_path.read_text(encoding="utf-8") == "# bundle"
        assert not list((tmp_path / "ctx").glob("*.tmp"))
        json.loads(json_path.read_text(encoding="utf-8"))  # 可解析 = 完整落盘
