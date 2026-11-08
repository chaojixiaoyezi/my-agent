"""R7-W5：活体形状的 owner 根/工作区成员符号链接不得让 retention 引用保护整轮失败；引用来源内的链接仍失败关闭。"""
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store.retention import apply_owner_retention
from agent_py_agent.tests.test_memory_retention_wave5 import NOW, old_file, runtime


# LLM: 走生产默认预算入口并按 scan_partial 续调，错误码按全部分片累计。
# 函数用途: 运行 owner retention 直到本周期结束，返回末片回执与累计错误码。
def _apply_until_settled(home):
    codes = []
    for _ in range(64):
        report = apply_owner_retention(home, now=NOW)
        codes.extend(error.error_code for error in report.errors)
        if not report.scan_partial:
            return report, codes
    pytest.fail("retention apply never settled")


# LLM: 两种形状均取自 PID 70255 owner 树：根目录 link_vol15 与工作区内部的散落链接。
# 函数用途: 证明成员枚举里的链接不再把整轮 pins 置为不完整。
@pytest.mark.parametrize("shape", ["owner_root", "workspace_member"])
def test_live_symlink_shapes_keep_retention_pins_complete(tmp_path, shape):
    home, _candidates, _service = runtime(tmp_path)
    owner = Path(home.owner_home_dir)
    target = owner / "tasks/2026-11-07/work/encyc"
    target.mkdir(parents=True)
    workspace = Path(home.owner_workspace_dir) / "runtime/workspaces/main-real"
    workspace.mkdir(parents=True)
    link = owner / "link_vol15" if shape == "owner_root" else workspace / "stray-link"
    link.symlink_to(target)
    report, codes = _apply_until_settled(home)
    assert "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" not in codes
    assert not report.scan_partial


# LLM: 引用来源文件本身是链接时不能被当作缺失或空集；保护必须失败关闭。
# 函数用途: 守住成员枚举放宽不外溢到 pin 来源读取。
def test_symlinked_pin_source_still_fails_closed(tmp_path):
    home, _candidates, _service = runtime(tmp_path)
    real = old_file(Path(tmp_path) / "outside.jsonl", '{"source_refs": ["evidence/a"]}\n')
    runs = Path(home.owner_memory_curator_runs_dir)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "pins.jsonl").symlink_to(real)
    _report, codes = _apply_until_settled(home)
    assert "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" in codes
