# LLM: Skill适配只读显式根的SKILL.md与skill_meta.json，不执行正文；更新只写侧元数据，公开字段共用AssetDescriptor过滤。
# 模块用途: 为Owner及已加载技能提供标准提取Key、稳定发现和不改正文的元数据更新。
from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import list_names_beneath, read_bytes_beneath, write_text_atomic_beneath
from ..settings.config_io import _strip_yaml_comment
from . import safe_asset_id
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


# LLM: YAML只作数据，支持标量、缩进映射/列表、块文本与JSON流容器；不执行tag/anchor或引入未声明依赖。
# 函数用途: 解析技能头部元数据，正文只供原解析器分隔，不作为命令或权限。
def _frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        raise ValueError('Skill YAML头缺少结束分隔符')
    tokens = [(len(line) - len(line.lstrip(' ')), _strip_yaml_comment(line).strip()) for line in lines[1:end]
              if _strip_yaml_comment(line).strip()]
    if not tokens:
        return {}
    meta, index = _yaml_block(tokens, 0, tokens[0][0])
    if not isinstance(meta, dict) or index != len(tokens):
        raise ValueError('Skill YAML头必须是完整对象')
    return meta


# LLM: 递归只消费缩进数据，深度有界且重复键拒绝，不解析对象tag或Python表达式。
# 函数用途: 解析技能YAML的列表与映射，保持访问目标/数据源/IO容器结构。
def _yaml_block(tokens, index, indent, depth=0):
    if depth > 32:
        raise ValueError('Skill YAML嵌套过深')
    sequence = tokens[index][1].startswith('- ')
    result = [] if sequence else {}
    while index < len(tokens) and tokens[index][0] == indent:
        raw = tokens[index][1]
        if sequence:
            if not raw.startswith('- '):
                raise ValueError('Skill YAML列表格式错误')
            value = raw[2:].strip()
            if re.match(r'[^: ]+:(?:\s|$)', value):
                tokens[index] = (indent + 2, value)
                item, index = _yaml_block(tokens, index, indent + 2, depth + 1)
            else:
                item, index = _yaml_item(tokens, index, indent, value, depth)
            result.append(item)
            continue
        key, separator, value = raw.partition(':')
        if not separator or key in result or not key.strip():
            raise ValueError('Skill YAML键无效或重复')
        result[key.strip()], index = _yaml_item(tokens, index, indent, value.strip(), depth)
    if index < len(tokens) and tokens[index][0] > indent:
        raise ValueError('Skill YAML缩进无效')
    return result, index


# LLM: 块文本和子容器仅在当前键的更深缩进内消费，后续同层键不被吞掉。
# 函数用途: 解析一个YAML值并返回下一条未消费记录。
def _yaml_item(tokens, index, indent, value, depth):
    following = index + 1
    if value in ('>', '|', '>-', '|-', '>+', '|+'):
        block = []
        while following < len(tokens) and tokens[following][0] > indent:
            block.append(tokens[following][1])
            following += 1
        return (' ' if value.startswith('>') else '\n').join(block), following
    if value:
        return _yaml_scalar(value), following
    if following < len(tokens) and tokens[following][0] > indent:
        return _yaml_block(tokens, following, tokens[following][0], depth + 1)
    return None, following


# LLM: 标量不eval；引号保留井号，JSON容器严格解析，无引号行内列表只接受普通标量；未知tag/anchor明确拒绝。
# 函数用途: 规范常用YAML标量和行内容器，拒绝不能安全解释的语法。
def _yaml_scalar(value):
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    if value.startswith('[') and value.endswith(']'):
        return [_yaml_scalar(item.strip()) for item in value[1:-1].split(',')] if value[1:-1].strip() else []
    if value in ('~', 'null'):
        return None
    if value.startswith(('!', '&', '*', '{', '"', "'")):
        raise ValueError('Skill YAML值无效或使用不支持的tag/anchor')
    return value


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


# LLM: 已合法ID保持原值；非ASCII/特殊名称只派生稳定逻辑ID，不把原名拼成未经验证的路径。
# 函数用途: 为中文、空格等技能目录名生成可供资产协议使用的确定性ID。
def _skill_id(name):
    try:
        return safe_asset_id(name)
    except ValueError:
        if not isinstance(name, str) or not name:
            raise ValueError('Skill名称或ID必须是非空字符串') from None
        slug = re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('_.-')[:72] or 'name'
        return f'skill_{slug}_{sha256(name.encode("utf-8")).hexdigest()[:16]}'


# LLM: 特殊目录身份固定从目录派生，不被metadata.id重定位；原名放name/refs.skill_dir，公共字段严格往返。
# 函数用途: 将技能元数据转成插件可显示的标准提取Key，不携带正文或磁盘路径。
def _descriptor(directory, meta, owner_id):
    extensions = {key: meta.get(key, default) for key, default in (('when_to_use', ''), ('category', 'general'))}
    if any(not isinstance(value, str) for value in extensions.values()):
        raise ValueError('Skill适用场景和分类必须是字符串')
    special = _skill_id(directory.name) != directory.name
    refs = meta.get('refs', {})
    if not isinstance(refs, dict):
        raise ValueError('Skill refs必须是对象')
    refs = {**refs, **({'skill_dir': directory.name} if special else {})}
    descriptor = AssetDescriptor(id=_skill_id(directory.name if special else meta.get('id', directory.name)), kind='skill',
        name=directory.name if special else meta.get('name') or directory.name,
        summary=meta.get('description', meta.get('summary', '')),
        version=str(meta.get('version', '1.0.0')), owner_id=owner_id, metadata=extensions,
        access_targets=meta.get('access_targets', ()), data_sources=meta.get('data_sources', ()),
        credential_refs=meta.get('credential_refs', ()), io_contract=meta.get('io_contract', {}),
        refs=refs, tags=meta.get('tags', ())).to_dict()
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


# LLM: 只遍历显式root下目录，逐层nofollow验证；可推迟顶层learned以保证普通目录优先，正文内停止扫描。
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
        except FileNotFoundError:
            continue
        if 'SKILL.md' in names:
            yield path.joinpath(*parts, 'SKILL.md')
            continue
        pending.extend((*parts, name) for name in reversed(names)
                       if not name.startswith('.') and not (skip_learned and not parts and name == 'learned')
                       and path.joinpath(*parts, name).is_dir())


# LLM: 普通skills目录先于skills/learned；旧home/learned及工作区.agent/skills仍可读，同名Owner目录只认第一份，extra仅沿显式授权。
# 函数用途: 按来源优先级枚举准确Skill路径，查找时不根据请求名拼接磁盘路径。
def _owner_skill_paths(home, extra_roots):
    seen = set()
    for root, skip in ((home / 'skills', True), (home / 'skills/learned', False),
                       (home / 'learned', False), (home / 'workspace/.agent/skills', False)):
        for path in _skill_paths(root, skip_learned=skip):
            if path.parent.name not in seen:
                seen.add(path.parent.name)
                yield path
    for root in extra_roots:
        yield from _skill_paths(root)


# LLM: list/get共用同一发现与ID去重事实；Owner根来自调用方，不读取引用字段作为路径或身份。
# 函数用途: 按固定来源优先级提取技能描述符和原目录名，避免详情读到已被遮蔽的副本。
def _owner_descriptors(owner_home, extra_roots):
    home, seen = Path(owner_home).absolute(), set()
    for path in _owner_skill_paths(home, extra_roots):
        descriptor = extract_skill_descriptor(path, owner_id=home.name)
        if descriptor['id'] not in seen:
            seen.add(descriptor['id'])
            yield path.parent.name, descriptor


# LLM: Owner skills及learned先于extra，同ID首项优先；排序不改变来源资格，不物化缺失Owner。
# 函数用途: 列出本Owner技能标准字典，支持额外已加载Skill目录或准确正文路径。
def list_owner_skill_descriptors(owner_home, extra_roots=()):
    return sorted((descriptor for _, descriptor in _owner_descriptors(owner_home, extra_roots)), key=lambda row: row['id'])


# LLM: 只匹配实际发现的原名或稳定ID，不用用户输入构造路径；缺失和损坏均显式返回错误。
# 函数用途: 按派生ID或原始目录名取得同一份Owner技能描述符。
def get_owner_skill_descriptor(owner_home, skill_id, extra_roots=()):
    for name, descriptor in _owner_descriptors(owner_home, extra_roots):
        if skill_id in (name, descriptor['id']):
            return descriptor
    raise FileNotFoundError('Owner技能不存在')
