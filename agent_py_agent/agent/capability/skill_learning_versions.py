# LLM: 自学版本引用沿原registry归属和snapshot授权，只固定已存在且hash相符的历史正文；不创建文件或接管手写Skill。
# 模块用途: 为自学Skill绑定不可变正文和同源撤回元信息；在途快照跨更新保留原版，撤回后仍拒读。
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..common.nofollow_fs import read_text_beneath
from .skill_learning_store import SkillLearningStore, SkillLearningStoreError
from .skill_snapshot import skill_content_sha256


# LLM: 每项必须同时匹配owner来源、canonical活动路径、registry hash、版本文件hash；失败不猜版本或创建副本。
# 函数用途: 在发现快照建立时冻结自学版本路径，普通Skill继续沿原读取行为。
def freeze_learned_snapshot(snapshot, home_paths, *, recommended_limit=50):
    if not getattr(home_paths, "owner_skill_learning_dir", None):
        return snapshot
    store = SkillLearningStore.for_home(home_paths)
    try:
        registry = store.load_registry()
    except SkillLearningStoreError:
        return replace(snapshot,entries=tuple(entry for entry in snapshot.entries if not Path(entry.path).is_relative_to(store.learned_root)))
    from .skill_learning_maintenance import recommended_learning
    recommended = recommended_learning(registry, recommended_limit)
    hidden = {name for operation in registry.import_operations.values() if operation.get("status") != "committed" for name in operation.get("target_names",[])}
    entries = []
    for entry in snapshot.entries:
        if entry.source == "owner" and entry.name in hidden:
            continue
        current = registry.skills.get(entry.name)
        expected = store.skill_path(entry.name)
        if current is None or entry.source != 'owner' or Path(entry.path) != expected or entry.content_sha256 != current.sha256:
            entries.append(entry)
            continue
        state = current.maintenance.get('state','trial')
        if state in {'disabled','archived'}:
            entries.append(replace(entry,enabled=False,recommended=False))
            continue
        entry = replace(entry,recommended=entry.name in recommended)
        version = store.versions_dir / entry.name / f'v{current.version}.md'
        try:
            text = read_text_beneath(store.directory, ('versions', entry.name, version.name))
            valid = text is not None and skill_content_sha256(text) == current.sha256
        except (OSError, UnicodeError):
            valid = False
        entries.append(replace(entry, frozen_path=str(version)) if valid else entry)
    return replace(snapshot, entries=tuple(entries))


# LLM: 元信息和正文读取共用原托管授权判据；读取registry和路径事实但不读正文，不把在途快照当成永久授权。
# 函数用途: 为自身发现返回自学Skill当前生命周期和是否仍可请求原固定版本。
def frozen_learned_metadata(entry):
    frozen = Path(entry.frozen_path)
    store = SkillLearningStore(frozen.parents[2], frozen.parents[4]/"skills")
    try:
        registry = store.load_registry()
    except SkillLearningStoreError as exc:
        raise OSError("learned skill registry unavailable") from exc
    record = registry.skills.get(entry.name)
    state = record.maintenance.get('state', 'trial') if record is not None else 'removed'
    authorized = bool(record is not None and state not in {'disabled', 'archived'}
                      and Path(entry.path) == store.skill_path(entry.name) and Path(entry.path).is_file())
    return {"state": state, "authorized": authorized,
            "revision": record.revision if record else None, "version": record.version if record else None}


# LLM: 固定正文不固定授权；原metadata判据仍拒绝删除、停用和路径替换，不增加fallback或读取活动正文。
# 函数用途: 在同源授权复核通过后读取原固定版本；拒绝语义保持不变。
def read_frozen_learned_body(entry):
    if not frozen_learned_metadata(entry)["authorized"]:
        raise FileNotFoundError('learned skill authorization no longer available')
    frozen = Path(entry.frozen_path)
    store = SkillLearningStore(frozen.parents[2], frozen.parents[4]/"skills")
    return read_text_beneath(store.directory, ('versions', entry.name, frozen.name))


# LLM: registry变化影响启停/导入可见性/推荐，必须进入快照缓存指纹；坏文件给固定不可用指纹，不授权正文。
# 函数用途: 读取原学习登记的内容hash用于失效缓存，不建立第二索引。
def learning_state_fingerprint(home_paths):
    if not getattr(home_paths,"owner_skill_learning_dir",None):
        return ""
    try:
        text = read_text_beneath(home_paths.owner_skill_learning_dir,("registry.json",))
        return skill_content_sha256(text) if text is not None else ""
    except (OSError,ValueError):
        return "learning-state-unavailable"


# LLM: 只有当前仍授权且已托管的同stable_id可按原任务冻结hash定位历史，不接受路径或改名；用户停用/删除仍拒绝。
# 函数用途: 在任务/子代理重启恢复时把准确历史引用重新绑定不可变正文，普通来源不走此路径。
def resolve_learned_revision(entry, expected_sha256):
    if not entry.frozen_path or entry.source != 'owner' or not entry.enabled:
        return None
    frozen = Path(entry.frozen_path)
    store = SkillLearningStore(frozen.parents[2],frozen.parents[4]/'skills')
    registry = store.load_registry()
    record = registry.skills.get(entry.name)
    if record is None or record.maintenance.get('state') in {'disabled','archived'}:
        return None
    for version in sorted((store.versions_dir/entry.name).glob('v*.md')):
        text = read_text_beneath(store.directory,('versions',entry.name,version.name))
        if text is not None and skill_content_sha256(text)==expected_sha256:
            return replace(entry,content_sha256=expected_sha256,frozen_path=str(version))
    return None
