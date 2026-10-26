# LLM: 普通发现只读明确根；有快照只投影enabled_entries，不补扫或执行正文。
# 模块用途: 共用安全codec和根声明提供技能资产投影及准确来源匹配。
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..capability.skills import _as_list, skill_catalog_roots, split_skill_frontmatter
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import list_names_beneath, read_bytes_beneath, write_text_atomic_beneath
from . import (
    asset_root_lock,
    canonical_skill_name,
    next_asset_revision,
    safe_asset_id,
    skill_asset_id,
    valid_skill_reference,
)
from .descriptor import AssetDescriptor, clean_json


# LLM: 不resolve模型路径以保留链接拒绝，输入必须是目录或准确SKILL.md，不从元数据取得路径。
# 函数用途: 规范单个技能位置，所有实际读写仍沿nofollow链执行。
def _directory(value):
    path = Path(value).absolute()
    if path.name == 'SKILL.md':
        return path.parent
    if path.suffix == '.md':
        raise ValueError('技能文件必须名为SKILL.md')
    return path


# LLM: 根须来自调用方授权；普通单链接文件有界读取，缺失可空，损坏和链接拒绝不能吞掉。
# 函数用途: 安全读取技能正文或侧元数据的UTF-8文本，不创建文件。
def _text(directory, filename):
    raw = read_bytes_beneath(directory, (filename,), max_bytes=4 * 1024 * 1024)
    return None if raw is None else raw.decode('utf-8')


# LLM: Card和资产提取只用一个有界安全codec。
# 函数用途: 从唯一frontmatter入口取得头部。
def _frontmatter(text):
    return split_skill_frontmatter(text)[0]


# LLM: JSON侧文件缺失是合法空扩展，非法JSON/非对象明确失败；不把坏内容覆盖成新对象。
# 函数用途: 读取同目录技能扩展，供提取及锁内更新复用。
def _sidecar(directory):
    text = _text(directory, 'skill_meta.json')
    value = {} if text is None else json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('skill_meta.json必须是对象')
    return value


# LLM: Owner身份由可信调用参数给出，frontmatter/sidecar不能扩权；先头部后侧文件覆盖，正文无回传。
# 函数用途: 提取单个技能的标准字典，支持目录和SKILL.md路径。
def extract_skill_descriptor(skill_dir_or_md_path, owner_id=''):
    directory = _directory(skill_dir_or_md_path)
    text = _text(directory, 'SKILL.md')
    if text is None:
        raise FileNotFoundError('Skill正文不存在')
    return _descriptor(directory, {**_frontmatter(text), **_sidecar(directory)}, owner_id)


# LLM: 共用资产键派生，已合法ID保持原值；非ASCII名称绝不直接拼成未验证路径。
# 函数用途: 为中文、空格等技能目录名生成可供资产协议使用的确定性ID。
def _skill_id(name):
    return skill_asset_id(name)


# LLM: 特殊目录身份仍从目录派生；enabled沿已合并头部/原侧文件的严格bool，出处保留在metadata，不授执行权。
# 函数用途: 将技能元数据转成插件可显示的标准提取Key，不携带正文或磁盘路径。
def _descriptor(directory, meta, owner_id):
    extensions = {key: meta.get(key, default) for key, default in (('when_to_use', ''), ('category', 'general'))}
    if any(not isinstance(value, str) for value in extensions.values()):
        raise ValueError('Skill适用场景和分类必须是字符串')
    summary = meta.get('description', meta.get('summary', ''))
    if not isinstance(summary, str):
        raise ValueError('Skill说明必须是字符串')
    special = _skill_id(directory.name) != directory.name
    refs = meta.get('refs', {})
    if not isinstance(refs, dict):
        raise ValueError('Skill refs必须是对象')
    refs = {**refs, **({'skill_dir': directory.name} if special else {})}
    descriptor = AssetDescriptor(id=_skill_id(directory.name if special else meta.get('id', directory.name)), kind='skill',
        name=directory.name if special else meta.get('name') or directory.name,
        summary=summary.strip(),
        version=str(meta.get('version', '1.0.0')), owner_id=owner_id,
        metadata={**meta.get('metadata', {}), **extensions}, enabled=meta.get('enabled', True),
        access_targets=meta.get('access_targets', ()), data_sources=meta.get('data_sources', ()),
        credential_refs=meta.get('credential_refs', ()), io_contract=meta.get('io_contract', {}),
        refs=refs, tags=_as_list(meta.get('tags'))).to_dict()
    return {**descriptor, **descriptor['metadata']}


# LLM: None保留、空容器清空；原路径锁保护读改写，全部验证后原子提交，未知侧元数据保留且统一去秘密键。
# 函数用途: 更新提取Key但绝不改SKILL.md正文，返回更新后的描述符。
def update_skill_extraction_keys(skill_dir, *, access_targets=None, data_sources=None, credential_refs=None, io_contract=None, tags=None):
    directory = _directory(skill_dir)
    if _text(directory, 'SKILL.md') is None:
        raise FileNotFoundError('Skill正文不存在')
    updates = {'access_targets': access_targets, 'data_sources': data_sources, 'credential_refs': credential_refs,
               'io_contract': io_contract, 'tags': tags}
    with locked_json_path(directory / 'skill_meta.json'):
        text = _text(directory, 'SKILL.md')
        if text is None:
            raise FileNotFoundError('Skill正文不存在')
        meta = {**_sidecar(directory), **{key: value for key, value in updates.items() if value is not None}}
        descriptor = _descriptor(directory, {**_frontmatter(text), **meta}, '')
        write_text_atomic_beneath(directory, ('skill_meta.json',), json.dumps(
            clean_json(meta), ensure_ascii=False, allow_nan=False) + '\n')
    return descriptor


# LLM: 只遍历显式root且逐层nofollow，坏子树隔离；中断不作为坏文件吞掉。
# 函数用途: 收集一个根目录中的准确SKILL.md路径，不创建目录或缓存。
def _skill_paths(root, *, skip_learned=False):
    path = Path(root).absolute()
    if path.name == 'SKILL.md':
        yield path
        return
    pending = [()]
    while pending:
        parts = pending.pop()
        try:
            names = sorted(list_names_beneath(path, parts))
        except InterruptedError:
            raise
        except OSError:
            continue
        if 'SKILL.md' in names:
            yield path.joinpath(*parts, 'SKILL.md')
            continue
        if len(parts) >= 6:
            continue
        pending.extend((*parts, name) for name in reversed(names)
                       if not name.startswith('.') and not (skip_learned and not parts and name == 'learned')
                       and path.joinpath(*parts, name).is_dir())


# LLM: 根共用服务声明；仅同来源同目录遮蔽，显式其它来源仍可查询。
# 函数用途: 枚举实际文件和准确来源，不拿请求ID拼路径。
def _catalog_paths(home, extra_roots=(), workspace_root=None):
    home, seen = Path(home).absolute(), set()
    for root, source in skill_catalog_roots(home, workspace_root, extra_roots):
        for path in _skill_paths(root, skip_learned=root == home / 'skills'):
            key = (source, path.parent.name)
            if key not in seen:
                seen.add(key)
                yield path, source


# LLM: 保留内部路径入口，不能用它绕过已提供的授权快照。
# 函数用途: 复用唯一根声明枚举路径。
def _owner_skill_paths(home, extra_roots, workspace_root=None):
    for path, _ in _catalog_paths(home, extra_roots, workspace_root):
        yield path


# LLM: 显式来源保留plugin包段，裸名仅在已发现别名内匹配。
# 函数用途: 判断准确来源引用是否命中真实身份。
def _reference_matches(reference, aliases, source):
    if ':' not in reference:
        return reference in aliases
    requested_source, _, name = reference.rpartition(':')
    return requested_source == source and name in aliases


# LLM: 来源、稳定引用和hash只来自发现事实，头部或sidecar不能覆盖。
# 函数用途: 为标准资产附加不可伪造的投影身份和可选正文。
def _catalog_projection(descriptor, source, stable_id, content_hash, text, include_body):
    descriptor = {**descriptor, 'metadata': {
        **descriptor['metadata'], 'source': source, 'stable_id': stable_id, 'content_hash': content_hash}}
    if include_body:
        descriptor['body_markdown'] = text
    return descriptor


# LLM: 来源和内容hash来自发现事实；稳定引用保留原生Unicode名称，安全资产ID仍从实际目录派生。
# 函数用途: 从发现文件生成来源固定的资产描述符。
def _catalog_descriptor(path, source, owner_id, include_body):
    text = _text(path.parent, 'SKILL.md')
    if text is None:
        raise FileNotFoundError('Skill正文不存在')
    meta = {**_frontmatter(text), **_sidecar(path.parent)}
    descriptor = _descriptor(path.parent, meta, owner_id)
    name = meta.get('name') or path.parent.name
    stable_id = valid_skill_reference(f'{source}:{name}')
    return _catalog_projection(descriptor, source, stable_id, sha256(text.encode('utf-8')).hexdigest(), text, include_body)


# LLM: 只沿快照read_body取授权版本，不读活动sidecar或重新发现；核对正文hash。
# 函数用途: 将已启用条目投影为资产，失效快照明确拒读。
def _snapshot_descriptor(home, snapshot, entry, include_body):
    stable_id = valid_skill_reference(entry.stable_id)
    if stable_id != f'{entry.source}:{entry.name}':
        raise ValueError('Skill快照身份不一致')
    try:
        text = snapshot.read_body(stable_id)
    except InterruptedError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError('已授权Skill快照正文不可读') from exc
    if sha256(text.encode('utf-8')).hexdigest() != entry.content_sha256:
        raise ValueError('Skill快照正文已变化')
    directory = Path(entry.path).parent
    meta = {**_frontmatter(text), 'id': _skill_id(directory.name),
            'name': entry.name, 'description': entry.description,
            'when_to_use': entry.when_to_use, 'category': entry.category, 'tags': list(entry.tags)}
    descriptor = _descriptor(directory, meta, home.name)
    return _catalog_projection(descriptor, entry.source, stable_id, entry.content_sha256, text, include_body)


# LLM: enabled_entries是唯一候选集合；别名只来自冻结逻辑名和实际目录，不拿别名拼路径或扩大来源。
# 函数用途: 投影授权快照并隔离无关损坏条目。
def _snapshot_descriptors(home, snapshot, requested, include_body):
    priority = {'workspace': 0, 'owner': 1, 'learned': 2, 'shared': 3, 'builtin': 4}
    entries = sorted(enumerate(snapshot.enabled_entries()),
                     key=lambda item: (priority.get(item[1].source, 5), item[0]))
    for _, entry in entries:
        if not entry.enabled:
            continue
        directory = Path(entry.path).parent.name
        aliases = (entry.name, directory, _skill_id(directory), _skill_id(entry.name))
        if requested is not None and not _reference_matches(requested, aliases, entry.source):
            continue
        try:
            descriptor = _snapshot_descriptor(home, snapshot, entry, include_body)
        except InterruptedError:
            raise
        except (ValueError, OSError) as exc:
            if requested is not None:
                raise ValueError('请求的Skill快照损坏或不可读') from exc
            continue
        yield Path(entry.path).parent.name, descriptor


# LLM: 有快照时不补扫；普通发现保留原稳定逻辑名、实际目录与安全键别名，来源仍精确匹配。
# 函数用途: 为列表和详情提供同一有序授权描述符流。
def _owner_descriptors(owner_home, extra_roots=(), *, workspace_root=None,
                       skill_snapshot=None, requested=None, include_body=False):
    home = Path(owner_home).absolute()
    if skill_snapshot is not None:
        yield from _snapshot_descriptors(home, skill_snapshot, requested, include_body)
        return
    for path, source in _catalog_paths(home, extra_roots, workspace_root):
        aliases = (path.parent.name, _skill_id(path.parent.name))
        try:
            descriptor = _catalog_descriptor(path, source, home.name, include_body)
        except InterruptedError:
            raise
        except (ValueError, OSError) as exc:
            if requested is not None and _reference_matches(requested, aliases, source):
                raise ValueError('请求的Skill文件损坏或不可读') from exc
            continue
        aliases += (descriptor['id'], descriptor['name'],
                    descriptor['metadata']['stable_id'].rsplit(':', 1)[-1],
                    canonical_skill_name(descriptor['metadata']['stable_id']))
        if requested is None or _reference_matches(requested, aliases, source):
            yield path.parent.name, descriptor


# LLM: 展示排序保持首个裸ID优先，显式来源详情不受列表去重影响；空快照不补扫。
# 函数用途: 列出当前授权技能的标准字典。
def list_owner_skill_descriptors(owner_home, extra_roots=(), *,
                                 workspace_root: str | Path | None = None,
                                 skill_snapshot: Any | None = None):
    found = {}
    for _, descriptor in _owner_descriptors(
        owner_home, extra_roots, workspace_root=workspace_root, skill_snapshot=skill_snapshot
    ):
        found.setdefault(descriptor['id'], descriptor)
    return [found[key] for key in sorted(found)]


# LLM: stable_id严格匹配来源，原目录名仅用于既有只读查找，不作为磁盘路径。
# 函数用途: 读取裸ID或准确来源技能，并可附带同一授权版本的正文。
def get_owner_skill_descriptor(owner_home, skill_id, extra_roots=(), *,
                                workspace_root: str | Path | None = None,
                                skill_snapshot: Any | None = None, include_body=False):
    if not isinstance(skill_id, str) or not skill_id:
        raise ValueError('Skill引用必须是非空字符串')
    if ':' in skill_id:
        valid_skill_reference(skill_id)
    for _, descriptor in _owner_descriptors(
        owner_home, extra_roots, workspace_root=workspace_root, skill_snapshot=skill_snapshot,
        requested=skill_id, include_body=include_body
    ):
        return descriptor
    raise FileNotFoundError('Owner技能不存在')


# LLM: 仅当前Owner原生skills目录可修改，builtin/shared/workspace不借裸ID扩权；正文与授权快照不重写。
# 类用途: 为跨用户投递提供原生技能启停入口，沿原侧元数据与资产恢复锁持久化。
class SkillAdapter:
    # LLM: 可信Owner根由调用方给出，构造不建目录、不扫描其它来源。
    # 函数用途: 绑定可管理的原生技能目录。
    def __init__(self, owner_home):
        self.owner_home = Path(owner_home).absolute()

    # LLM: 配额先于原资产根/侧文件锁；侧文件布尔值为唯一运行开关覆盖，旧快照读取仍复核此文件。
    # 函数用途: 切换本人的一个既有技能，保留正文、来源和未知侧元数据。
    def set_enabled(self, skill_id, enabled, *, admission=None):
        from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
        from .workspace_assets import _strict_skill_paths

        safe_asset_id(skill_id)
        if type(enabled) is not bool:
            raise ValueError('enabled必须是布尔值')
        home = self.owner_home
        if admission is None:
            with owner_quota_enforcer_from_policy(home).admission() as quota:
                return self.set_enabled(skill_id, enabled, admission=quota)
        with asset_root_lock(home):
            matches = [path.parent for path in _strict_skill_paths(home)
                if extract_skill_descriptor(path)['id'] == skill_id]
            if len(matches) != 1:
                raise ValueError('Owner技能不存在或身份不唯一')
            directory = matches[0]
            with locked_json_path(directory / 'skill_meta.json'):
                meta = _sidecar(directory)
                text = _text(directory, 'SKILL.md')
                if text is None:
                    raise FileNotFoundError('Skill正文不存在')
                meta.update(enabled=enabled, revision=next_asset_revision(meta))
                descriptor = _descriptor(directory, {**_frontmatter(text), **meta}, meta.get('owner_id', ''))
                raw = json.dumps(clean_json(meta), ensure_ascii=False, allow_nan=False) + '\n'
                admission.check((OwnerQuotaChange(directory / 'skill_meta.json', len(raw.encode())),))
                write_text_atomic_beneath(home, (*directory.relative_to(home).parts, 'skill_meta.json'), raw)
        return descriptor
