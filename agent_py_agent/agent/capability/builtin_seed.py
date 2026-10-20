# LLM: 本模块的共享镜像/索引写者消费原globalguard；Skill读取和owner私有初始化不在本门，默认原指纹与完整镜像合同保持。
# 模块用途: 私有重建共享内置Skill及管理员派生索引；受管root/父目录no-follow，不替代运行时SkillsService。
"""把 skill 同步进 home,让内置和用户自定义 skill 都自动可见、可被检索。

两类 skill:
- 内置(随仓库发布,源码 skills/builtin/):全量镜像到 home/shared/builtin/。
- 用户自定义(用户自己丢进 home/shared/skills/):原地不动,只扫描索引。

每次 ensure_my_agent_home 时:① 把源码内置 skill 镜像到 shared/builtin(覆盖同名、删源码
已移除的);② 扫描 shared/builtin + shared/skills 下所有 SKILL.md,合并写进
shared/indexes/skills.jsonl 作为管理员可读的派生清单。运行时发现只走唯一 SkillsService，
不读取这个索引。fingerprint 幂等:内置源码或用户 skill 任一改动才重建,否则跳过。
fingerprint 标记写 cache、不进 skill 目录,保证 shared/builtin 零中间态文件。
tools/workflows/role_templates 暂无统一文件格式,不在此自动索引。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from ..common.json_io import jsonl_lines
from ..common.nofollow_fs import ensure_private_dir

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


# LLM: 镜像只改shared/builtin；原globalguard后先核root/父目录no-follow，再原删除/完整复制；新受管目录0700，copy2源权限不变。
# 函数用途: 安全重建共享内置Skill镜像，拒绝受管链接，不chmod用户目录或放宽W只读门。
def _mirror_builtin(src: Path, shared_builtin_dir: Path) -> None:
    """全量镜像源码内置 skill 到 shared/builtin(清旧重建)。只动 shared/builtin。"""
    from ..common.global_write_guard import require_global_writer

    require_global_writer("shared_builtin_skills")
    ensure_private_dir(shared_builtin_dir, canonical_root=shared_builtin_dir.parent)
    if shared_builtin_dir.exists():
        shutil.rmtree(shared_builtin_dir, ignore_errors=True)
    ensure_private_dir(shared_builtin_dir, canonical_root=shared_builtin_dir.parent)
    for skill_dir in _skill_dirs(src):
        target = shared_builtin_dir / skill_dir.relative_to(src)
        ensure_private_dir(target.parent, canonical_root=shared_builtin_dir)
        shutil.copytree(skill_dir, target)


def _count_index_lines(skills_index_jsonl: Path) -> int:
    if not skills_index_jsonl.is_file():
        return 0
    # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
    return sum(1 for line in jsonl_lines(skills_index_jsonl.read_text(encoding="utf-8")) if line.strip())


# LLM: 本入口写共享镜像/索引/fingerprint，受限actor在读取快路前拒绝；运行时SkillsService不调用它。
# 函数用途: 由原默认/C同步共享Skill派生索引，Count0原指纹幂等与字节保持。
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
    from ..common.global_write_guard import require_global_writer

    require_global_writer("shared_skill_index")
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
