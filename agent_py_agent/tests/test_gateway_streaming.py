"""Gateway 流式 chunk 文件测试。"""

import json
import time
from pathlib import Path

from agent_py_agent.agent.gateway_parts.paths import GatewayPaths, gateway_chunk_path
from agent_py_agent.agent.gateway_parts.request_execution import close_chunk_stream


def _make_paths(tmp_path: Path) -> GatewayPaths:
    processing = tmp_path / "requests" / "processing"
    processing.mkdir(parents=True, exist_ok=True)
    return GatewayPaths(
        root=tmp_path,
        pid=tmp_path / "gateway.pid",
        adapter_pid=tmp_path / "adapter.pid",
        state=tmp_path / "gateway_state.json",
        heartbeat=tmp_path / "gateway_heartbeat.json",
        stop_request=tmp_path / "gateway_stop.request",
        log=tmp_path / "gateway.log",
        inbox=tmp_path / "requests" / "pending",
        processing=processing,
        done=tmp_path / "requests" / "done",
        failed=tmp_path / "requests" / "failed",
        responses=tmp_path / "responses",
        history=tmp_path / "gateway_requests.jsonl",
    )


def test_gateway_chunk_path_is_in_processing(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "req-123")
    assert chunk_path == paths.processing / "req-123.chunks.jsonl"


def test_chunk_file_write_and_read(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "test-req")

    # Simulate daemon writing chunks
    with open(chunk_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "text": "你好"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"t": time.time(), "text": "世界"}, ensure_ascii=False) + "\n")

    # Simulate CLI reading chunks
    lines = chunk_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    obj0 = json.loads(lines[0])
    obj1 = json.loads(lines[1])
    assert obj0["text"] == "你好"
    assert obj1["text"] == "世界"


def test_chunk_file_cleanup(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "test-req")

    # Write some chunks
    with open(chunk_path, "a", encoding="utf-8") as f:
        f.write('{"t": 1, "text": "x"}\n')

    assert chunk_path.exists()
    chunk_path.unlink(missing_ok=True)
    assert not chunk_path.exists()


def test_close_chunk_stream_keeps_file_for_late_pollers(tmp_path):
    paths = _make_paths(tmp_path)
    chunk_path = gateway_chunk_path(paths, "test-req")
    chunk_path.write_text('{"t": 1, "text": "x"}\n', encoding="utf-8")

    close_chunk_stream(chunk_path)

    assert chunk_path.exists()
