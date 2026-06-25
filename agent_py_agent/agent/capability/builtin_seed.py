"""把随仓库发布的内置 skill 镜像到用户 home 的 shared/builtin + 写 skills.jsonl 索引。

内置 skill 物理在源码 skills/builtin/(随版本走),用户在 home 目录里看不到、也无法和
自定义 skill 统一管理。本模块在 ensure_my_agent_home 时把内置 skill 全量镜像到
home/shared/builtin/ 并写 home/shared/indexes/skills.jsonl,使内置 skill 像自定义 skill
一样在 home 可见、可被 capability 索引发现。

全量同步:覆盖同名、删除源码已移除的(home/shared/builtin 是内置专属镜像;用户自定义
skill 放 home/shared/skills,不受影响)。版本更新后自动跟随、不漂移。fingerprint 幂等:
源码内容未变则整体跳过(每次启动都跑,必须便宜);fingerprint 标记写在 home 外的工作
目录(cache),**不污染 shared/builtin——那里只留纯粹的 skill,没有任何中间态文件**。
tools/workflows 不在此列。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

# capability/builtin_seed.py -> parents[2] = agent_py_agent
_BUILTIN_SRC = Path(__file__).resolve().parents[2] / "skills" / "builtin"


def _builtin_skill_dirs(src: Path) -> list[Path]:
    if not src.is_dir():
        return []
    return sorted({skill_md.parent for skill_md in src.rglob("SKILL.md")})


def _fingerprint(skill_dirs: list[Path], src: Path) -> str:
    """源码内置 skill 的内容指纹:相对路径 + 文件字节,任一 skill 增删改即变。"""
    files = sorted(
        file for skill_dir in skill_dirs for file in skill_dir.rglob("*") if file.is_file()
    )
    digest = hashlib.sha256()
    for file in files:
        digest.update(str(file.relative_to(src)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def sync_builtin_skills_to_home(
    shared_builtin_dir: Path, skills_index_jsonl: Path, fingerprint_file: Path
) -> int:
    """全量镜像内置 skill 到 home/shared/builtin + 写 skills.jsonl 索引,返回 skill 数。

    幂等:源码 fingerprint 与 fingerprint_file 相同则跳过(不删建、不重写)。fingerprint
    标记写在调用方指定的工作目录(cache),**不进 shared/builtin**——保证那里只留纯粹的
    skill 镜像、零中间态文件。半成品(中途被打断)不写 fingerprint,下次启动自动重建。
    home/shared/skills(用户自定义)全程不受影响。
    """
    from .skills import parse_skill_file

    src = _BUILTIN_SRC
    skill_dirs = _builtin_skill_dirs(src)
    fingerprint = _fingerprint(skill_dirs, src)
    if fingerprint_file.is_file() and fingerprint_file.read_text(encoding="utf-8").strip() == fingerprint:
        return len(skill_dirs)

    # 全量覆盖:清旧镜像重建。只动 shared/builtin(内置专属),用户自定义在 shared/skills。
    if shared_builtin_dir.exists():
        shutil.rmtree(shared_builtin_dir, ignore_errors=True)
    shared_builtin_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, object]] = []
    for skill_dir in skill_dirs:
        relative = skill_dir.relative_to(src)
        dest = shared_builtin_dir / relative
        shutil.copytree(skill_dir, dest)
        card = parse_skill_file(dest / "SKILL.md", source="builtin")
        records.append(
            {
                "id": card.name,
                "name": card.name,
                "kind": "skill",
                "source": "builtin",
                "path": str(dest / "SKILL.md"),
                "description": card.description,
            }
        )

    skills_index_jsonl.parent.mkdir(parents=True, exist_ok=True)
    skills_index_jsonl.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    fingerprint_file.parent.mkdir(parents=True, exist_ok=True)
    fingerprint_file.write_text(fingerprint, encoding="utf-8")
    return len(records)
