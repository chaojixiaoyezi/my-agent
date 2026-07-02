"""交付区扫描 vendor 排除钉子测试（A1-u1 实锤：node_modules 1.7 万文件灌满
扫描上限，真产物被挤出 artifacts 清单）。只影响扫描/展示，不删文件。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
    _artifacts_from_task_output_scan,
)


def test_task_output_scan_skips_vendor_dirs_but_keeps_real_artifacts():
    with tempfile.TemporaryDirectory() as td:
        output = Path(td) / "output"
        (output / "node_modules" / "lodash").mkdir(parents=True)
        (output / "node_modules" / "lodash" / "index.js").write_text("module.exports = {}\n", encoding="utf-8")
        (output / ".git").mkdir()
        (output / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (output / "__pycache__").mkdir()
        (output / "__pycache__" / "app.cpython-314.txt").write_text("cache\n", encoding="utf-8")
        (output / "src").mkdir()
        (output / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (output / "README.md").write_text("# 项目\n", encoding="utf-8")

        artifacts = _artifacts_from_task_output_scan(
            [{"path": output, "kind": "dir", "scope": "task_output"}]
        )
        paths = {item["path"] for item in artifacts}
        assert str(output / "src" / "app.py") in paths
        assert str(output / "README.md") in paths
        assert not any("node_modules" in path for path in paths)
        assert not any(".git" in path for path in paths)
        assert not any("__pycache__" in path for path in paths)
        # 文件本身还在磁盘上(只排除展示,不删)。
        assert (output / "node_modules" / "lodash" / "index.js").is_file()
