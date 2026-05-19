# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""provides CLI bootstrap helpers for config loading, agent construction, routing, and formatting.

给人看的解释：
所有命令都会反复做几件事：加载配置、创建 SimpleAgent、创建能力路由、格式化时间。
这些公共动作统一放这里，避免每个命令模块各写一遍。
"""

import sys
import time
from pathlib import Path, PureWindowsPath

from ..agent.capabilities import CapabilityRouter
from ..agent.config import load_config
from ..agent.core import SimpleAgent
from ..agent.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "agent_config.yaml"
DEFAULT_CAPABILITY_CONFIG = ROOT / "config" / "capability_config.yaml"
CHAT_PROMPT = "你> "
FALLBACK_CHAT_PROMPT = "user> "


# LLM: add_resume_context_switches 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 注册 argparse 参数和子命令，决定用户可见的命令形状。
def add_resume_context_switches(command) -> None:

    group = command.add_mutually_exclusive_group()
    group.add_argument("--resume-context", dest="resume_context", action="store_true", help="本次请求临时启用恢复上下文注入")
    group.add_argument("--no-resume-context", dest="resume_context", action="store_false", help="本次请求临时关闭恢复上下文注入")
    command.set_defaults(resume_context=None)


# LLM: configure_stdio 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def configure_stdio() -> None:

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            _reconfigure_stdio_stream(stream_name, reconfigure)


# LLM: _reconfigure_stdio_stream 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _reconfigure_stdio_stream(stream_name: str, reconfigure) -> None:
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(
            f"stdio reconfigure failed stream={stream_name} error_code={type(exc).__name__} error={exc}",
            file=sys.__stderr__,
        )


# LLM: make_agent 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def make_agent(args) -> SimpleAgent:

    config = load_config(args.config)
    roots = resolve_workspace_roots(config, args.config)
    return SimpleAgent(config, roots[0], workspace_roots=roots)


# LLM: resolve_workspace_root 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def resolve_workspace_root(config, config_path: str | Path, *, current_dir: str | Path | None = None) -> Path:
    return resolve_workspace_roots(config, config_path, current_dir=current_dir)[0]


# LLM: resolve_workspace_roots 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def resolve_workspace_roots(config, config_path: str | Path, *, current_dir: str | Path | None = None) -> list[Path]:

    raw_value = getattr(config, "workspace_root", "")
    raw_roots = _raw_workspace_roots(raw_value)
    cwd = Path(current_dir).expanduser().resolve() if current_dir is not None else ROOT
    roots: list[Path] = []
    for raw in raw_roots:
        candidate = _resolve_one_workspace_root(raw, config_path, current_dir=cwd)
        if candidate is not None and candidate not in roots:
            roots.append(candidate)
    return roots or [cwd]


# LLM: _raw_workspace_roots 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _raw_workspace_roots(raw_value: object) -> list[object]:
    if isinstance(raw_value, list):
        return raw_value
    return [raw_value]


# LLM: _resolve_one_workspace_root 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _resolve_one_workspace_root(
    raw_value: object,
    config_path: str | Path,
    *,
    current_dir: Path,
) -> Path | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return current_dir
    if _is_foreign_windows_absolute_path(raw):
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(config_path).expanduser().resolve().parent / candidate
    return candidate.resolve()

# LLM: _is_foreign_windows_absolute_path keeps CLI path parsing from treating foreign drive roots as local workspaces.
# 函数用途: 判断一个看起来像 Windows 绝对路径的字符串，避免在当前平台被错误解析成可用 workspace 路径。
def _is_foreign_windows_absolute_path(raw: str) -> bool:
    path = Path(raw)
    if path.is_absolute():
        return False
    windows_path = PureWindowsPath(raw)
    return bool(windows_path.drive and windows_path.root)


# LLM: make_capability_router 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def make_capability_router(agent: SimpleAgent, capability_config, skill_dirs: list[str] | None):

    default_skill_dirs = [ROOT.parent / "skills", ROOT / "skills"]
    dirs = [Path(item).expanduser() for item in skill_dirs] if skill_dirs else default_skill_dirs
    skills = SkillRegistry(dirs)
    skills.scan()
    return CapabilityRouter(
        config=capability_config,
        skill_registry=skills,
        tool_specs=agent.tools.specs(),
    )


# LLM: format_local_time 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def format_local_time(timestamp: float) -> str:

    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


# LLM: resume_context_override 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def resume_context_override(args) -> bool | None:

    return getattr(args, "resume_context", None)


# LLM: _memory_record_count 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _memory_record_count(agent: SimpleAgent) -> int:
    try:
        return len(agent.memory.all())
    except Exception as exc:
        print(
            f"memory count failed error_code={type(exc).__name__} error={exc}",
            file=sys.stderr,
        )
        return 0
