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
    """LLM: add tri-state CLI switches for optional recovery context injection.

    给人看的解释：
    不传参数就按配置走；`--resume-context` 临时打开；`--no-resume-context` 临时关闭。
    这样测试恢复能力时不用反复改 YAML。
    """

    group = command.add_mutually_exclusive_group()
    group.add_argument("--resume-context", dest="resume_context", action="store_true", help="本次请求临时启用恢复上下文注入")
    group.add_argument("--no-resume-context", dest="resume_context", action="store_false", help="本次请求临时关闭恢复上下文注入")
    command.set_defaults(resume_context=None)


def configure_stdio() -> None:
    """把标准输出尽量固定到 UTF-8。

    这样做主要是为了避免 Windows 终端在打印模型返回内容时再次乱码。
    说白了，就是先把'字能不能正常显示'这个基础问题兜住。
    """

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
    """根据配置创建一个可直接运行的智能体实例。"""

    config = load_config(args.config)
    return SimpleAgent(config, resolve_workspace_root(config, args.config))


def resolve_workspace_root(config, config_path: str | Path) -> Path:
    """解析本次运行实际使用的工作区根目录。

    默认仍然使用包目录 `agent_py_agent`，保持之前行为不变。配置里写了
    `workspace_root` 时，memory、gateway、subagent 账本和文件工具都会落在该目录下。
    场景测试会利用这个开关把真实 API 任务关进临时 fixture，避免碰当前开发仓库。
    """

    raw = str(getattr(config, "workspace_root", "") or "").strip()
    if not raw:
        return ROOT
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(config_path).expanduser().resolve().parent / candidate
    return candidate.resolve()


def make_capability_router(agent: SimpleAgent, capability_config, skill_dirs: list[str] | None):
    """创建 capability router，合并当前工具和可选 skill 目录。"""

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
    """把 Unix 时间戳格式化成人能扫一眼的本地时间。"""

    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def resume_context_override(args) -> bool | None:
    """LLM: read the tri-state CLI override for automatic recovery context.

    给人看的解释：
    命令行有三种状态：没传参数就返回 None，表示按配置走；
    传 `--resume-context` 返回 True，传 `--no-resume-context` 返回 False。
    """

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
