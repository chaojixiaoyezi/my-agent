# LLM: 随包 Skill 的唯一定位规则：只看安装表里 phase=active 的激活，路径只由 owner、激活环境编号与包声明的入口模块推出，
#   不接受包提供的路径；目录内容随插件 wheel（或 v6 随包文件）一起经哈希校验安装。停用、卸载或换代后下一次 Skill 快照自然不再包含它。
#   修改时同步 capability/skill_service.py 的插件根顺序与 test_plugin_skills。
# 模块用途: 给 Skill 目录提供"已启用插件自带的 Skill 目录"清单，让 Skill 随插件装卸。

from __future__ import annotations

import logging
import os
import sysconfig
from pathlib import Path

from .plugin_entry import FILES_DIRECTORY
from .plugin_install_store import PluginInstallStore

logger = logging.getLogger(__name__)
PLUGIN_SKILL_SOURCE_PREFIX = "plugin:"


# LLM: Python 插件环境与宿主同一 Python 版本（环境准备时已校验），因此可用宿主 sysconfig 规则推出其 purelib，跨平台一致。
#   必须显式指定 venv 布局的 scheme：宿主默认 scheme 在 Homebrew/framework Python（osx_framework_library）或 user 安装下
#   会忽略传入的 base，把目录算到宿主 site-packages，插件随包 Skill 就此全部失踪（2026-09-25 对照线在 Homebrew 3.14 上发现）。
#   v6 非 Python 包的 Skill 随包文件解包在 files/skills 下（包描述构造时已核对 SKILL.md 与声明的 Skill 一致）。
# 函数用途: 计算某个插件激活环境里的 skills 目录；路径永远落在该插件的激活环境内。
def plugin_skill_dir(owner, installation) -> Path:
    environment = owner.plugins_dir / "environments" / installation.activation.plan.environment_ref
    if getattr(installation.manifest, "entry", None) is not None:
        return environment / FILES_DIRECTORY / "skills"
    python = environment / "python"
    purelib = sysconfig.get_path(
        "purelib", scheme=_environment_scheme(), vars={"base": str(python), "platbase": str(python)},
    )
    return Path(purelib) / installation.manifest.entry_module.split(".")[0] / "skills"


# LLM: 3.11+ 的 venv scheme 就是激活环境的真实布局；3.10 没有它，用 posix_prefix / nt 等价表达，不读宿主默认 scheme。
# 函数用途: 返回插件激活环境（一个 venv）的 sysconfig 布局名。
def _environment_scheme() -> str:
    if "venv" in sysconfig.get_scheme_names():
        return "venv"
    return "nt" if os.name == "nt" else "posix_prefix"


# LLM: 只读安装表快照，不启动插件进程；表不可读时记录类型并返回空（Skill 缺席不影响核心）。
# 函数用途: 列出当前 owner 所有已启用且声明了 Skill 的插件的 (目录, 来源标签)。
def enabled_plugin_skill_roots(owner) -> tuple[tuple[Path, str], ...]:
    if owner is None:
        return ()
    try:
        entries = PluginInstallStore(owner).snapshot()
    except (OSError, ValueError) as exc:
        logger.warning("插件安装目录不可读，本轮不提供插件 Skill：%s", type(exc).__name__)
        return ()
    roots = []
    for entry in entries:
        if entry.enabled and getattr(entry.manifest, "skills", ()):
            roots.append((plugin_skill_dir(owner, entry), PLUGIN_SKILL_SOURCE_PREFIX + entry.manifest.plugin_id))
    return tuple(roots)
