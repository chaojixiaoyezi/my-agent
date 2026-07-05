"""P3 交付占位密度结构闸:交付代码占位记号密度明显过高 → 收尾打回一次(宽松阈值)。

判据全结构化(铁律):TODO/FIXME 只认注释语境行(防 todo 应用的标识符/界面文案误伤),
中文占位词任意位置;总数 >= 50 且密度 >= 12/KLOC 才打回;幂等一次+双出口;永不抛错。
真机标定:u-code-b 421/12.5K 行(≈34/KLOC,该拦)vs u-code-a 64/15K 行(≈4/KLOC,别拦)。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.delivery_closeout.placeholder_density import (
    placeholder_density_rework,
)


def _request(output_dir: Path, tool_context: list | None = None):
    params = SimpleNamespace(
        tool_context=tool_context if tool_context is not None else [],
        task_attributes={"run_workspace": {"task_root": str(output_dir.parent), "output_dir": str(output_dir)}},
    )
    return SimpleNamespace(params=params)


def _write_code(output_dir: Path, name: str, lines: list[str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / name).write_text("\n".join(lines), encoding="utf-8")


def _stuffed_lines(total: int, todo_every: int) -> list[str]:
    return [
        (f"// TODO: 实现第{i}块" if i % todo_every == 0 else f"const v{i} = {i};")
        for i in range(total)
    ]


def test_stuffed_delivery_reworked_once_with_structured_payload(tmp_path):
    """u-code-b 形态(≈34/KLOC):打回一次,payload 带计数/密度/最密文件;二次同形态放行。"""
    out = tmp_path / "output"
    _write_code(out, "app.js", _stuffed_lines(2000, 30))  # ~67 处注释 TODO / 2000 行 ≈ 33/KLOC
    request = _request(out)

    assert placeholder_density_rework(request, {}) is True
    marker_entries = [item for item in request.params.tool_context if "[placeholder-density-rework]" in item]
    assert len(marker_entries) == 1
    payload = json.loads(marker_entries[0].split("\n", 1)[1])
    assert payload["placeholder_markers"] >= 50
    assert payload["density_per_kloc"] >= 12.0
    assert payload["top_files"] and payload["top_files"][0]["path"].endswith("app.js")
    assert placeholder_density_rework(request, {}) is False  # 幂等:额度已花


def test_honest_delivery_not_reworked(tmp_path):
    """u-code-a 形态(≈4/KLOC,合理 TODO 量):不打回。"""
    out = tmp_path / "output"
    _write_code(out, "app.js", _stuffed_lines(15000, 235))  # ~64 处 / 15000 行 ≈ 4.3/KLOC
    request = _request(out)

    assert placeholder_density_rework(request, {}) is False
    assert request.params.tool_context == []


def test_todo_outside_comment_context_not_counted(tmp_path):
    """todo 应用护栏:标识符/界面文案里的 TODO 不算占位——只有注释语境的才计数。"""
    out = tmp_path / "output"
    lines = []
    for i in range(1000):
        lines.append(f'const todoItem{i} = "TODO List 第{i}项";')  # 非注释语境:不算
    _write_code(out, "todo_app.js", lines)
    request = _request(out)

    assert placeholder_density_rework(request, {}) is False


def test_chinese_placeholder_markers_counted_anywhere(tmp_path):
    """中文占位词(此处省略/待实现/占位)任意位置计数:塞满空壳照拦。"""
    out = tmp_path / "output"
    lines = [(f'render("此处省略第{i}段实现");' if i % 20 == 0 else f"let x{i} = {i};") for i in range(2000)]
    _write_code(out, "shell.js", lines)  # 100 处 / 2000 行 = 50/KLOC
    request = _request(out)

    assert placeholder_density_rework(request, {}) is True


def test_small_delivery_spared_by_absolute_floor(tmp_path):
    """小交付绝对阈值兜底:密度高但总数 < 50(几处 TODO 的小脚本)不打回。"""
    out = tmp_path / "output"
    _write_code(out, "tiny.py", ["# TODO: 补测试"] * 10 + ["x = 1"] * 40)  # 10 处,密度 200/KLOC
    request = _request(out)

    assert placeholder_density_rework(request, {}) is False


def test_non_code_files_ignored(tmp_path):
    """报告/文档不沾:md 里的 TODO 章节是合法散文,只扫代码扩展名。"""
    out = tmp_path / "output"
    out.mkdir(parents=True)
    (out / "report.md").write_text("\n".join(["- TODO: 后续工作"] * 200), encoding="utf-8")
    request = _request(out)

    assert placeholder_density_rework(request, {}) is False


def test_never_raises_on_garbage_inputs(tmp_path):
    assert placeholder_density_rework(object(), {}) is False
    assert placeholder_density_rework(SimpleNamespace(params=None), None) is False
    # 有 tool_context 但没有 run_workspace:扫不出文件 → 安静放行。
    request = SimpleNamespace(params=SimpleNamespace(tool_context=[], task_attributes=None))
    assert placeholder_density_rework(request, {"artifacts": [{"ok": True, "path": "/nonexistent/x.py"}]}) is False
