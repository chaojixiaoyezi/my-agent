
from __future__ import annotations

"""provides CLI bootstrap helpers for config loading, agent construction, routing, and formatting.

给人看的解释：
所有命令都会反复做几件事：加载配置、创建 SimpleAgent、创建能力路由、格式化时间。
这些公共动作统一放这里，避免每个命令模块各写一遍。
"""

import sys
import time
from pathlib import Path, PureWindowsPath

from ..agent.capability import CapabilityRouter
from ..agent.capability.skills import SkillRegistry
from ..agent.core import SimpleAgent
from ..agent.settings import load_config
from ..agent.settings.services.runtime_config_env import apply_runtime_config_environment

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "agent_config.yaml"
DEFAULT_CAPABILITY_CONFIG = ROOT / "config" / "capability_config.yaml"
CHAT_PROMPT = "你> "
PLAIN_CHAT_PROMPT = "user> "


def add_resume_context_switches(command) -> None:

    group = command.add_mutually_exclusive_group()
    group.add_argument("--resume-context", dest="resume_context", action="store_true", help="本次请求临时启用恢复上下文注入")
    group.add_argument("--no-resume-context", dest="resume_context", action="store_false", help="本次请求临时关闭恢复上下文注入")
    command.set_defaults(resume_context=None)


def configure_stdio() -> None:

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            _reconfigure_stdio_stream(stream_name, reconfigure)


def _reconfigure_stdio_stream(stream_name: str, reconfigure) -> None:
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(
            f"stdio reconfigure failed stream={stream_name} error_code={type(exc).__name__} error={exc}",
            file=sys.__stderr__,
        )


def make_agent(args) -> SimpleAgent:

    config = apply_runtime_config_environment(load_config(args.config))
    explicit_root = _explicit_workspace_root(args)
    if explicit_root is not None:
        config.workspace_root = str(explicit_root)
    roots = resolve_workspace_roots(config, args.config)
    return SimpleAgent(config, roots[0], workspace_roots=roots)


def _explicit_workspace_root(args) -> Path | None:
    raw = getattr(args, "workspace_root", None)
    if not isinstance(raw, (str, Path)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


def resolve_workspace_root(config, config_path: str | Path, *, current_dir: str | Path | None = None) -> Path:
    return resolve_workspace_roots(config, config_path, current_dir=current_dir)[0]


def resolve_workspace_roots(config, config_path: str | Path, *, current_dir: str | Path | None = None) -> list[Path]:

    raw_value = getattr(config, "workspace_root", "")
    raw_roots = _raw_workspace_roots(raw_value)
    cwd = Path(current_dir).expanduser().resolve() if current_dir is not None else Path.cwd().resolve()
    roots: list[Path] = []
    for raw in raw_roots:
        candidate = _resolve_one_workspace_root(raw, config_path, current_dir=cwd)
        if candidate is not None and candidate not in roots:
            roots.append(candidate)
    return roots or [cwd]


def _raw_workspace_roots(raw_value: object) -> list[object]:
    if isinstance(raw_value, list):
        return raw_value
    return [raw_value]


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

def _is_foreign_windows_absolute_path(raw: str) -> bool:
    path = Path(raw)
    if path.is_absolute():
        return False
    windows_path = PureWindowsPath(raw)
    return bool(windows_path.drive and windows_path.root)


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


def format_local_time(timestamp: float) -> str:

    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def resume_context_override(args) -> bool | None:

    return getattr(args, "resume_context", None)


def _memory_record_count(agent: SimpleAgent) -> int:
    try:
        return len(agent.memory.all())
    except Exception as exc:
        print(
            f"memory count failed error_code={type(exc).__name__} error={exc}",
            file=sys.stderr,
        )
        return 0
