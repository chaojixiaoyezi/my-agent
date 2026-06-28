"""F11① 文件路径透明归一:_relocate_escape_abs_path 纯函数测试。

场景:per-user / admin agent 用 write_file 写一个绝对路径到所有合法根之外的无害位置(写飞,
如 /root/monitor_lab/x.py)。期望:透明搬进当前任务 task_output_dir 下、保留路径结构,
工具照常成功、agent 无感。但越权(别人 owner home)和危险目录(/etc 等)返回空 → 不归一,
留给现有 owner 墙 / 危险目录机制硬拦(归一它们会把硬拦偷换成静默成功,绝不能做)。落在任一已声明
合法根(工作区 + task_output_dir/task_work_dir 等任务边界根)内的写入也不归一(本就合法)。

用 tmp_path 造一套 owner home 布局;纯函数不依赖 MY_AGENT_HOME / 文件存在与否,只看路径关系。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling.registry_invoke import (
    EscapeRelocateRequest,
    _relocate_escape_abs_path,
)


def _layout(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / ".my-agent"
    owners_root = home / "owners"
    owner_a = owners_root / "feishu" / "A"  # 自己的 owner home(scope)
    owner_b = owners_root / "feishu" / "B"  # 别人的 owner home
    output = owner_a / "tasks" / "t1" / "output"  # task_output_dir
    return {
        "home": home,
        "owners_root": owners_root,
        "owner_a": owner_a,
        "owner_b": owner_b,
        "output": output,
    }


def _req(
    *,
    raw: object,
    tool_name: str,
    owner_scope_root: object,
    task_output_dir: object,
    legal_roots: list[Path] | None = None,
) -> EscapeRelocateRequest:
    return EscapeRelocateRequest(
        raw=raw,
        tool_name=tool_name,
        owner_scope_root=owner_scope_root,
        task_output_dir=task_output_dir,
        legal_roots=legal_roots or [],
        dangerous_roots=["/etc"],
    )


def test_relocate_escape_abs_path_into_output(tmp_path) -> None:
    """写飞:write_file 写所有合法根外的绝对路径 → 归一进 task_output_dir,保留结构。"""
    layout = _layout(tmp_path)
    escape = tmp_path / "monitor_lab" / "x.py"  # 合法根外的无害绝对路径
    result = _relocate_escape_abs_path(_req(
        raw=str(escape),
        tool_name="write_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir=str(layout["output"]),
    ))
    assert result
    resolved = Path(result).resolve(strict=False)
    out_dir = layout["output"].resolve(strict=False)
    assert resolved.is_relative_to(out_dir)  # 在 output 下
    assert str(resolved).endswith("monitor_lab/x.py")  # 保留路径结构


def test_relocate_inside_output_no_rewrite(tmp_path) -> None:
    """合法区内(task_output_dir 内)→ 不动,返回空。"""
    layout = _layout(tmp_path)
    target = layout["output"] / "report.md"
    result = _relocate_escape_abs_path(_req(
        raw=str(target),
        tool_name="write_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir=str(layout["output"]),
    ))
    assert result == ""


def test_relocate_inside_task_work_dir_no_rewrite(tmp_path) -> None:
    """回归(本应不动):task_work_dir 等已声明边界根内的绝对写入不归一——例如子代理 runner
    写 output.json 到 task_work,legal_roots 含 task_work 时必须原样落地、不被搬走。"""
    layout = _layout(tmp_path)
    task_work = layout["output"].parent / "work"
    target = task_work / "agents" / "run-1" / "output.json"
    result = _relocate_escape_abs_path(_req(
        raw=str(target),
        tool_name="write_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir=str(layout["output"]),
        legal_roots=[task_work],  # 写边界系统认可的合法根
    ))
    assert result == ""


def test_relocate_inside_owner_home_no_rewrite(tmp_path) -> None:
    """owner home 内(非 task 目录,如 memory)→ 合法,返回空。"""
    layout = _layout(tmp_path)
    target = layout["owner_a"] / "memory" / "long_term.jsonl"
    result = _relocate_escape_abs_path(_req(
        raw=str(target),
        tool_name="write_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir=str(layout["output"]),
    ))
    assert result == ""


def test_relocate_inside_workspace_root_no_rewrite(tmp_path) -> None:
    """合法区内(任一 legal_roots / workspace_roots 下)→ 不动,返回空。"""
    layout = _layout(tmp_path)
    ws = tmp_path / "ws"
    target = ws / "src" / "a.py"
    result = _relocate_escape_abs_path(_req(
        raw=str(target),
        tool_name="write_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir=str(layout["output"]),
        legal_roots=[ws],
    ))
    assert result == ""


def test_relocate_read_tool_no_rewrite(tmp_path) -> None:
    """读类工具不归一(只对写类工具),返回空。"""
    layout = _layout(tmp_path)
    escape = tmp_path / "monitor_lab" / "x.py"
    result = _relocate_escape_abs_path(_req(
        raw=str(escape),
        tool_name="read_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir=str(layout["output"]),
    ))
    assert result == ""


def test_relocate_relative_path_no_rewrite(tmp_path) -> None:
    """相对路径交给现有相对归一,本函数不管,返回空。"""
    layout = _layout(tmp_path)
    result = _relocate_escape_abs_path(_req(
        raw="output/x.py",
        tool_name="write_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir=str(layout["output"]),
    ))
    assert result == ""


def test_relocate_cross_owner_not_rewritten(tmp_path) -> None:
    """越权:写别人 owner home(owners 根下、非自己 scope)→ 不归一,留给 owner 墙拦。"""
    layout = _layout(tmp_path)
    target = layout["owner_b"] / "SOUL.md"
    result = _relocate_escape_abs_path(_req(
        raw=str(target),
        tool_name="write_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir=str(layout["output"]),
    ))
    assert result == ""


def test_relocate_dangerous_root_not_rewritten(tmp_path) -> None:
    """危险目录:写 /etc/x → 不归一,留给危险目录机制拦。"""
    layout = _layout(tmp_path)
    result = _relocate_escape_abs_path(_req(
        raw="/etc/x.conf",
        tool_name="write_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir=str(layout["output"]),
    ))
    assert result == ""


def test_relocate_no_task_output_dir_no_rewrite(tmp_path) -> None:
    """task_output_dir 为空(没任务上下文)→ 不归一,返回空。"""
    layout = _layout(tmp_path)
    escape = tmp_path / "monitor_lab" / "x.py"
    result = _relocate_escape_abs_path(_req(
        raw=str(escape),
        tool_name="write_file",
        owner_scope_root=str(layout["owner_a"]),
        task_output_dir="",
    ))
    assert result == ""
