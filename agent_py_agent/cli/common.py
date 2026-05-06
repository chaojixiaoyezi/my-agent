from __future__ import annotations

"""LLM: provides CLI bootstrap helpers for config loading, agent construction, routing, and formatting.

给人看的解释：
所有命令都会反复做几件事：加载配置、创建 SimpleAgent、创建能力路由、格式化时间。
这些公共动作统一放这里，避免每个命令模块各写一遍。
"""

import sys
import time
from pathlib import Path

from ..agent.capabilities import CapabilityRouter
from ..agent.config import load_config
from ..agent.core import SimpleAgent
from ..agent.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "agent_config.yaml"
DEFAULT_CAPABILITY_CONFIG = ROOT / "config" / "capability_config.yaml"
CHAT_PROMPT = "你> "
FALLBACK_CHAT_PROMPT = "user> "


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
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception as exc:
                print(
                    f"stdio reconfigure failed stream={stream_name} error_code={type(exc).__name__} error={exc}",
                    file=sys.__stderr__,
                )


def make_agent(args) -> SimpleAgent:

    config = load_config(args.config)
    roots = resolve_workspace_roots(config, args.config)
    return SimpleAgent(config, roots[0], workspace_roots=roots)


def resolve_workspace_root(config, config_path: str | Path) -> Path:
    return resolve_workspace_roots(config, config_path)[0]


def resolve_workspace_roots(config, config_path: str | Path) -> list[Path]:

    raw_value = getattr(config, "workspace_root", "")
    raw_roots = _raw_workspace_roots(raw_value)
    empty_means_default = isinstance(raw_value, list)
    roots: list[Path] = []
    for raw in raw_roots:
        candidate = _resolve_one_workspace_root(raw, config_path, empty_means_default=empty_means_default)
        if candidate is not None and candidate not in roots:
            roots.append(candidate)
    return roots or [ROOT]


def _raw_workspace_roots(raw_value: object) -> list[object]:
    if isinstance(raw_value, list):
        return raw_value
    return [raw_value]


def _resolve_one_workspace_root(
    raw_value: object,
    config_path: str | Path,
    *,
    empty_means_default: bool,
) -> Path | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return ROOT if empty_means_default else None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(config_path).expanduser().resolve().parent / candidate
    return candidate.resolve()


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
