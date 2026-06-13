"""run_command 后台执行钉子(P0-2 对照能力补齐:持久/后台 shell)。

钉死契约:run_in_background 启动后台进程立即返回 pid+output_file 不阻塞;
输出落 .background_jobs/ 日志;registry 留痕;参数各形态识别;同步模式不受影响。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_py_agent.agent.tooling.shell import ShellTool, _wants_background


def test_background_start_returns_pid_and_output_file(tmp_path):
    res = ShellTool(tmp_path).execute({"command": "echo started; sleep 0.2; echo done", "run_in_background": True})
    assert res.ok
    payload = json.loads(res.output)
    assert payload["status"] == "started" and payload["pid"] > 0
    out = Path(payload["output_file"])
    assert out.parent.name == ".background_jobs"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "done" not in out.read_text(encoding="utf-8"):
        time.sleep(0.1)
    content = out.read_text(encoding="utf-8")
    assert "started" in content and "done" in content, "后台输出必须落到 output_file"


def test_background_does_not_block(tmp_path):
    # 后台跑 1s 真实任务(非纯 sleep,避免被 wait 引导拦),execute 必须立即返回
    start = time.monotonic()
    res = ShellTool(tmp_path).execute(
        {"command": 'python3 -c "import time; time.sleep(1); print(1)"', "run_in_background": True}
    )
    elapsed = time.monotonic() - start
    assert res.ok and elapsed < 0.8, f"后台模式不得阻塞,实际耗时 {elapsed:.2f}s"


def test_background_records_registry(tmp_path):
    ShellTool(tmp_path).execute({"command": "echo hi", "run_in_background": True})
    registry = tmp_path / ".background_jobs" / "registry.jsonl"
    assert registry.exists()
    row = json.loads(registry.read_text(encoding="utf-8").splitlines()[0])
    assert row["pid"] > 0 and "hi" in row["command"]


def test_wants_background_param_forms():
    assert _wants_background({"run_in_background": True}) is True
    assert _wants_background({"run_in_background": "true"}) is True
    assert _wants_background({"run_in_background": "1"}) is True
    assert _wants_background({"run_in_background": False}) is False
    assert _wants_background({"run_in_background": "no"}) is False
    assert _wants_background({}) is False


def test_sync_mode_unaffected(tmp_path):
    res = ShellTool(tmp_path).execute({"command": "echo sync-result"})
    assert res.ok and "sync-result" in res.output
    assert not (tmp_path / ".background_jobs").exists(), "同步模式不创建后台目录"
