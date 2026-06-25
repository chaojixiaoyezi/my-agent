"""把 skill 同步进 home,让内置和用户自定义 skill 都自动可见、可被检索。

两类 skill:
- 内置(随仓库发布,源码 skills/builtin/):全量镜像到 home/shared/builtin/。
- 用户自定义(用户自己丢进 home/shared/skills/):原地不动,只扫描索引。

每次 ensure_my_agent_home 时:① 把源码内置 skill 镜像到 shared/builtin(覆盖同名、删源码
已移除的);② 扫描 shared/builtin + shared/skills 下所有 SKILL.md,合并写进
shared/indexes/skills.jsonl,capability 层据此发现。fingerprint 幂等:内置源码或用户 skill
任一改动才重建,否则跳过。fingerprint 标记写 cache、不进 skill 目录,保证 shared/builtin
零中间态文件。tools/workflows/role_templates 暂无统一文件格式,不在此自动索引。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

# capability/builtin_seed.py -> parents[2] = agent_py_agent
_BUILTIN_SRC = Path(__file__).resolve().parents[2] / "skills" / "builtin"


def _skill_dirs(root: Path) -> list[Path]:
    """root 下所有含 SKILL.md 的目录(任意层级)。"""
    if not root.is_dir():
        return []
    return sorted({skill_md.parent for skill_md in root.rglob("SKILL.md")})


def _iter_files(root: Path) -> list[Path]:
    return sorted(file for file in root.rglob("*") if file.is_file())


def _fingerprint(roots: list[Path]) -> str:
    """多个 skill 根的内容指纹:相对路径 + 文件字节,任一 skill 增删改即变。"""
    digest = hashlib.sha256()
    for root in roots:
        if not root.is_dir():
            digest.update(b"<absent>\0")
            continue
        for file in _iter_files(root):
            digest.update(str(file.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(file.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _index_record(skill_dir: Path, source: str) -> dict[str, object]:
    from .skills import parse_skill_file

    skill_md = skill_dir / "SKILL.md"
    card = parse_skill_file(skill_md, source=source)
    return {
        "id": card.name,
        "name": card.name,
        "kind": "skill",
        "source": source,
        "path": str(skill_md),
        "description": card.description,
    }


def _mirror_builtin(src: Path, shared_builtin_dir: Path) -> None:
    """全量镜像源码内置 skill 到 shared/builtin(清旧重建)。只动 shared/builtin。"""
    if shared_builtin_dir.exists():
        shutil.rmtree(shared_builtin_dir, ignore_errors=True)
    shared_builtin_dir.mkdir(parents=True, exist_ok=True)
    for skill_dir in _skill_dirs(src):
        shutil.copytree(skill_dir, shared_builtin_dir / skill_dir.relative_to(src))


def _count_index_lines(skills_index_jsonl: Path) -> int:
    if not skills_index_jsonl.is_file():
        return 0
    return sum(1 for line in skills_index_jsonl.read_text(encoding="utf-8").splitlines() if line.strip())


def sync_skill_index(
    shared_builtin_dir: Path,
    shared_skills_dir: Path,
    skills_index_jsonl: Path,
    fingerprint_file: Path,
) -> int:
    """镜像内置 skill 到 shared/builtin + 扫描 builtin/custom 合并写 skills.jsonl 索引。

    返回索引里的 skill 总数(内置 + 用户自定义)。fingerprint 幂等:源码内置或用户自定义
    任一改动才重建,fingerprint 写 fingerprint_file(cache,不进 skill 目录)。半成品(中途
    被打断)不写 fingerprint,下次启动自动重建。
    """
    src = _BUILTIN_SRC
    fingerprint = _fingerprint([src, shared_skills_dir])
    if fingerprint_file.is_file() and fingerprint_file.read_text(encoding="utf-8").strip() == fingerprint:
        return _count_index_lines(skills_index_jsonl)

    _mirror_builtin(src, shared_builtin_dir)

    records = [_index_record(d, "builtin") for d in _skill_dirs(shared_builtin_dir)]
    records += [_index_record(d, "custom") for d in _skill_dirs(shared_skills_dir)]

    skills_index_jsonl.parent.mkdir(parents=True, exist_ok=True)
    skills_index_jsonl.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    fingerprint_file.parent.mkdir(parents=True, exist_ok=True)
    fingerprint_file.write_text(fingerprint, encoding="utf-8")
    return len(records)
