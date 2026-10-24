# LLM: 显示批只共享单次registry观察，不缓存正文授权；根来自宿主冻结entry，不接受模型路径。
# 模块用途: 对同一受信自学store批量生成生命周期显示元信息。
import re
from pathlib import Path

from .skill_learning_store import SkillLearningStore, SkillLearningStoreError


# LLM: 根推导保持旧冻结路径布局；先验证名称与versions结构，不能按活动路径另找登记表。
# 函数用途: 从受信冻结引用还原原store，不读盘或创建目录。
def learned_store(entry):
    frozen = Path(entry.frozen_path)
    if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', entry.name)
            or len(frozen.parents) < 5 or frozen.parent.name != entry.name
            or frozen.parents[1].name != 'versions' or not re.fullmatch(r'v[1-9][0-9]*\.md', frozen.name)):
        raise ValueError('invalid frozen learned reference')
    return SkillLearningStore(frozen.parents[2], frozen.parents[4] / 'skills')


# LLM: 单项与批量共用判据；registry已做名称/layout解析，保留canonical活动路径和is_file检查。
# 函数用途: 从一份登记事实投影单项状态，不读活动或版本正文。
def metadata_from_registry(entry, store, registry):
    record = registry.skills.get(entry.name)
    state = record.maintenance.get('state', 'trial') if record is not None else 'removed'
    root = store.skills_root if record is not None and record.layout == 'owner' else store.learned_root
    expected = root / entry.name / 'SKILL.md'
    authorized = bool(record is not None and state not in {'disabled', 'archived'}
                      and Path(entry.path) == expected and Path(entry.path).is_file())
    return {'state': state, 'authorized': authorized,
            'revision': record.revision if record else None, 'version': record.version if record else None}


# LLM: 仅聚合调用方传入的授权子集；每根一次加载，错误单列None，不跨调用保留登记或授权。
# 函数用途: 一次列表查询批量读取自学元信息，避免每项两次整表解析。
def learned_metadata_batch(entries):
    groups, result = {}, {}
    for entry in entries:
        if not entry.frozen_path:
            continue
        try:
            store = learned_store(entry)
            groups.setdefault((store.directory, store.skills_root), []).append(entry)
        except (OSError, ValueError, IndexError):
            result[entry.stable_id] = None
    for (directory, skills_root), group in groups.items():
        store = SkillLearningStore(directory, skills_root)
        try:
            registry = store.load_registry()
        except (OSError, SkillLearningStoreError, ValueError):
            result.update((entry.stable_id, None) for entry in group)
            continue
        for entry in group:
            try:
                result[entry.stable_id] = metadata_from_registry(entry, store, registry)
            except (OSError, ValueError):
                result[entry.stable_id] = None
    return result
