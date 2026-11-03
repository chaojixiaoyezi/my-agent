"""Skill 管理与自学习工作台动作；只投影原生 Skill 文件与既有候选确认链。

写入只落在当前 owner 的 ``skills/<id>/SKILL.md``，沿 owner 配额、目录锁与 no-follow
原子写；候选列表、完整预览、确认和拒绝不新建第二份候选账本，分别复用原生
``/memory candidates`` 控制回执与 ``data/skill_proposals`` 提案记录。因此人工确认
始终绑定原 owner/actor/thread、用户实际看到的完整预览版本和目标版本 CAS。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..capability.skills import skill_catalog_roots
from ..common.nofollow_fs import list_names_beneath, read_bytes_beneath, write_text_atomic_beneath
from ..conversation.business_access import BusinessError
from ..gateway_parts.business_api_v13 import (
    v13_require_access,
    v13_resolve_space,
    v13_start_model_job,
)
from .v13_store import v13_store_for

# 预览令牌只用于本进程内绑定本次用户看到的版本，不是持久账本；进程重启后旧令牌自然失效。
_PREVIEW_KEY = os.urandom(32)
_PREVIEW_RE = re.compile(r'spv1-([0-9]{10})-([0-9a-f]{24})([0-9a-f]{16})([0-9a-f]{16})')
_SKILL_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


# LLM: 时间只来自本次调用注入的时钟，持久与展示都使用统一 UTC。
# 函数用途: 生成契约要求的 UTC 时间戳。
def _now(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: 每条记录读取前复核实时授权与 owner 归属，文件位置不等于访问权。
# 函数用途: 在当前动作边界上做实时权限末核。
def _require(call, record=None):
    v13_require_access(call, record)


# LLM: 规范 owner 路径只由可信调用上下文给出，不从参数拼接。
# 函数用途: 返回当前调用绑定的 owner home 路径对象。
def _home_paths(call):
    paths = getattr(getattr(call.context, 'host', None), 'home_paths', None)
    if paths is None or getattr(paths, 'owner_id', '') != call.owner_id:
        raise BusinessError('HOST_NOT_READY')
    return paths


# LLM: 内置 Skill 随仓库发布，只作为只读层参与列表，不在 owner 目录内写入。
# 函数用途: 返回内置技能根声明，缺失时为空。
def _builtin_roots():
    import agent_py_agent

    root = Path(agent_py_agent.__file__).resolve().parent / 'skills' / 'builtin'
    return ((root, 'builtin'),) if root.is_dir() else ()


# LLM: 自学 Skill 的归属只由原生登记表回答，不能凭目录位置推测。
# 函数用途: 读取当前 owner 的自学 Skill 名称集合。
def _learned_names(call):
    from ..capability.skill_learning_store import SkillLearningStore

    try:
        registry = SkillLearningStore.for_home(_home_paths(call)).load_registry()
    except (OSError, ValueError, RuntimeError) as error:
        raise BusinessError('HOST_NOT_READY') from error
    return set(registry.skills)


# LLM: 只有 owner 目录下的条目可能是自学 Skill，其它来源按原生来源名投影。
# 函数用途: 计算一条 Skill 的层。
def _layer(descriptor, learned):
    source = descriptor['metadata']['source']
    name = descriptor['metadata']['stable_id'].rsplit(':', 1)[-1]
    if source == 'owner':
        return 'learned' if name in learned else 'user'
    if source in ('workspace', 'builtin', 'learned'):
        return source
    return 'workspace'


# LLM: 版本引用必须来自本次实际正文，不能用展示名或更新时间代替。
# 函数用途: 返回一个 Skill 的正文摘要版本。
def _version_ref(descriptor):
    value = descriptor['metadata'].get('content_hash') or ''
    if not isinstance(value, str) or not value:
        raise BusinessError('HOST_NOT_READY')
    return value


# LLM: 目录判断不跟随链接，链接目录交给后续 no-follow 读叶明确失败。
# 函数用途: 判断技能目录内的一个相对分量是否为真实目录。
def _is_directory(root, parts):
    path = Path(root).joinpath(*parts)
    return path.is_dir() and not path.is_symlink()


# LLM: 只枚举目录名，不跟随目录链接；实际读取仍由 no-follow 读叶复核。
# 函数用途: 列出技能目录内的相对资源路径。
def _resource_paths(directory, *, limit=200):
    rows, pending = [], [()]
    while pending and len(rows) < limit:
        parts = pending.pop()
        for name in sorted(list_names_beneath(directory, parts)):
            if name.startswith('.'):
                continue
            child = (*parts, name)
            try:
                if _is_directory(directory, child):
                    if len(child) < 4:
                        pending.append(child)
                    continue
            except OSError:
                continue
            if child != ('SKILL.md',):
                rows.append('/'.join(child))
    return rows[:limit]


# LLM: 资源正文只给可解码的文本文件，超限时仍返回大小但不返回内容。
# 函数用途: 按契约投影技能目录内的文本资源。
def _resource_views(directory):
    views = []
    for path in _resource_paths(directory):
        try:
            raw = read_bytes_beneath(directory, tuple(path.split('/')), max_bytes=4 * 1024 * 1024)
        except (OSError, ValueError):
            continue
        if raw is None:
            continue
        text = None
        if len(raw) <= 64 * 1024:
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                text = None
        views.append({'path': path, 'size_bytes': len(raw), 'text': text})
    return views


# LLM: 列表条目同样来自原生发现顺序，只在投影时补目录与层。
# 函数用途: 把原生描述符投影成契约 SkillView。
def _view(descriptor, *, learned, index=None):
    metadata = descriptor.get('metadata') or {}
    directory = None
    if index:
        for key in (descriptor['name'], str(metadata.get('stable_id') or '').rsplit(':', 1)[-1],
                    descriptor['id'].rsplit(':', 1)[-1]):
            directory = index.get(key)
            if directory is not None:
                break
    return {'skill_id': descriptor['id'], 'name': descriptor['name'],
            'description': descriptor.get('summary') or '', 'layer': _layer(descriptor, learned),
            'enabled': bool(descriptor.get('enabled', True)), 'version_ref': _version_ref(descriptor),
            'tags': list(descriptor.get('tags') or []),
            'resources': _resource_paths(directory) if directory is not None else []}


# LLM: 目录索引只服务展示，不替代原生发现；一次列出只遍历一遍。
# 函数用途: 为当前 owner 建目录名到技能目录的映射。
def _directory_index(call):
    index, roots = {}, skill_catalog_roots(Path(call.owner_home).absolute(), None, _builtin_roots())
    for root, _source in roots:
        pending = [()]
        while pending:
            parts = pending.pop()
            names = list_names_beneath(root, parts)
            if 'SKILL.md' in names:
                index.setdefault(root.joinpath(*parts).name, root.joinpath(*parts))
                continue
            if len(parts) >= 4:
                continue
            for name in names:
                if name.startswith('.'):
                    continue
                try:
                    if _is_directory(root, (*parts, name)):
                        pending.append((*parts, name))
                except OSError:
                    continue
    return index


# LLM: 发现走原生适配器，列表与详情的来源顺序和去重规则保持一致。
# 函数用途: 返回当前 owner 可见的原生 Skill 描述符。
def _catalog(call):
    from ..assets.skill_adapter import list_owner_skill_descriptors

    return list_owner_skill_descriptors(call.owner_home, _builtin_roots())


# LLM: 详情按原生引用解析，缺失和坏文件都明确失败，不返回空成功。
# 函数用途: 读取一个 Skill 的原生描述符和正文。
def _descriptor(call, skill_id):
    from ..assets.skill_adapter import get_owner_skill_descriptor

    try:
        return get_owner_skill_descriptor(call.owner_home, skill_id, _builtin_roots(), include_body=True)
    except (FileNotFoundError, ValueError, OSError) as error:
        raise BusinessError('RESOURCE_NOT_FOUND') from error


# LLM: 不同层的同名 Skill 按工作区、用户、内置、自学依次遮蔽，列表与详情指向同一行。
# 函数用途: 按层优先级去重并排序原生描述符。
def _ordered(descriptors, learned):
    rank = {'workspace': 0, 'user': 1, 'builtin': 2, 'learned': 3}
    chosen = {}
    for descriptor in descriptors:
        layer = _layer(descriptor, learned)
        key = descriptor['name']
        if key not in chosen or rank[layer] < rank[chosen[key][0]]:
            chosen[key] = (layer, descriptor)
    return sorted(chosen.values(), key=lambda item: (rank[item[0]], item[1]['name'], item[1]['id']))


# LLM: 筛选条件、主体和动作共同绑定游标；改动任一条件都会让旧游标明确过期。
# 函数用途: 用进程内签名游标分页返回已投影的行。
def _page(rows, binding, *, cursor, limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise BusinessError('INVALID_SCHEMA')
    token_binding = hashlib.sha256(json.dumps([binding, rows], ensure_ascii=False, sort_keys=True,
                                              separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    position = 0
    if cursor is not None:
        try:
            token = json.loads(cursor)
            if (type(token) is not dict or set(token) != {'binding', 'position', 'signature'}
                    or token['binding'] != token_binding or type(token['position']) is not int
                    or not 0 <= token['position'] <= len(rows) or type(token['signature']) is not str):
                raise ValueError('cursor')
            expected = hmac.digest(_PREVIEW_KEY, json.dumps([token_binding, token['position']]).encode(),
                                   'sha256').hex()
            if not hmac.compare_digest(expected, token['signature']):
                raise ValueError('cursor')
            position = token['position']
        except (ValueError, TypeError, KeyError) as error:
            raise BusinessError('CURSOR_EXPIRED') from error
    end = min(position + limit, len(rows))
    next_cursor = None
    if end < len(rows):
        signature = hmac.digest(_PREVIEW_KEY, json.dumps([token_binding, end]).encode(), 'sha256').hex()
        next_cursor = json.dumps({'binding': token_binding, 'position': end, 'signature': signature},
                                 separators=(',', ':'))
    return {'items': rows[position:end], 'next_cursor': next_cursor,
            'coverage': 'partial' if next_cursor else 'complete'}


# LLM: 只读列表按层优先级与原生来源；分页游标绑定筛选条件，不能跨条件复用。
# 函数用途: 列出当前 owner 可见的 Skill。
def list_skills(call):
    _require(call)
    if call.params.get('space_id'):
        v13_resolve_space(call, call.params['space_id'])
    learned = _learned_names(call)
    index = _directory_index(call)
    rows = [_view(descriptor, learned=learned, index=index)
            for _layer_name, descriptor in _ordered(_catalog(call), learned)]
    layer = call.params.get('layer')
    enabled = call.params.get('enabled')
    query = call.params.get('query')
    if layer:
        rows = [row for row in rows if row['layer'] == layer]
    if enabled is not None:
        rows = [row for row in rows if row['enabled'] is enabled]
    if query:
        needle = query.casefold()
        rows = [row for row in rows if needle in row['name'].casefold() or needle in row['description'].casefold()]
    limit = call.params.get('limit')
    return _page(rows, [call.owner_id, call.action, layer, enabled, query],
                 cursor=call.params.get('cursor'), limit=50 if limit is None else limit)


# LLM: 正文与资源都从同一原生目录读取，缺失返回 404 而不是空成功。
# 函数用途: 读取一个 Skill 的正文和文本资源。
def get_skill(call):
    _require(call)
    if call.params.get('space_id'):
        v13_resolve_space(call, call.params['space_id'])
    learned = _learned_names(call)
    descriptor = _descriptor(call, call.params['skill_id'])
    index = _directory_index(call)
    view = _view(descriptor, learned=learned, index=index)
    directory = index.get(descriptor['name'])
    body = descriptor.get('body_markdown') or ''
    return {'skill': view, 'body_markdown': body[:200000],
            'resources': _resource_views(directory) if directory is not None else []}


# LLM: 目录名会成为真实路径分量，只接受单段安全字符，避免穿越与链接目标。
# 函数用途: 校验并返回可写入的技能目录名。
def _safe_skill_id(value):
    text = str(value or '')
    if _SKILL_ID_RE.fullmatch(text) is None or text in {'.', '..'}:
        raise BusinessError('INVALID_SCHEMA')
    return text


# LLM: 校验只在 owner 临时目录内解析，不写目录、不执行正文；失败逐项回报。
# 函数用途: 用原生解析器校验一份 SKILL.md 文本。
def _parse_body(call, body, skill_id=None):
    from ..capability.skills import parse_skill_file, split_skill_frontmatter

    if not isinstance(body, str):
        raise BusinessError('INVALID_SCHEMA')
    name = _safe_skill_id(skill_id) if skill_id else 'validation'
    root = Path(tempfile.mkdtemp(prefix='.v13-skill-', dir=str(call.owner_home)))
    try:
        path = root / 'skills' / name / 'SKILL.md'
        write_text_atomic_beneath(root, ('skills', name, 'SKILL.md'), body)
        try:
            card = parse_skill_file(path, require_frontmatter=True)
            meta, _body = split_skill_frontmatter(body)
        except (ValueError, OSError) as error:
            return {'valid': False,
                    'errors': [{'path': 'frontmatter', 'code': 'INVALID_FRONTMATTER',
                                'message': str(error)[:1000]}],
                    'warnings': [], 'descriptor': None}
    finally:
        shutil.rmtree(root, ignore_errors=True)
    warnings = []
    if not str(meta.get('name') or '').strip():
        warnings.append('未声明 name，使用技能ID作为名称')
    if not str(meta.get('when_to_use') or '').strip():
        warnings.append('未声明 when_to_use，模型可能难以判断适用场景')
    if skill_id and str(meta.get('name') or '').strip() not in {'', name}:
        warnings.append('frontmatter name 与技能ID不同，展示名以 frontmatter 为准')
    descriptor = {'name': card.name, 'description': card.description,
                  'when_to_use': card.when_to_use, 'category': card.category,
                  'scope': card.scope, 'tags': list(card.tags),
                  'risk_level': card.risk_level, 'enabled': bool(card.enabled)}
    return {'valid': True, 'errors': [], 'warnings': warnings[:50], 'descriptor': descriptor}


# LLM: 只报告结构化错误，不在响应里回显路径或正文。
# 函数用途: 校验一份待写入的 Skill 文本。
def validate(call):
    _require(call)
    return _parse_body(call, call.params['body_markdown'], call.params.get('skill_id'))


# LLM: 准入后在同一个原生资产锁内重读目标、比较CAS并做live末核；原子nofollow写不越owner。
# 函数用途: 原子写入一个用户层 Skill 正文。
def _write_skill(call, skill_id, body, *, creating=False):
    from ..assets import asset_root_lock
    from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy

    home = Path(call.owner_home)
    parts = ('skills', skill_id, 'SKILL.md')
    with owner_quota_enforcer_from_policy(home).admission() as admission:
        with asset_root_lock(home):
            existing = _existing_text(call, skill_id)
            if creating:
                if existing is not None and existing != body:
                    raise BusinessError('STATE_CONFLICT')
            else:
                descriptor = _descriptor(call, skill_id)
                if _layer(descriptor, _learned_names(call)) != 'user':
                    raise BusinessError('STATE_CONFLICT')
                if _version_ref(descriptor) != call.params['expected_version_ref']:
                    raise BusinessError('REVISION_CONFLICT')
            admission.check([OwnerQuotaChange(home.joinpath(*parts), len(body.encode('utf-8')))])
            _require(call)
            if existing != body:
                write_text_atomic_beneath(home, parts, body)
            return {'skill': _view(_descriptor(call, skill_id), learned=_learned_names(call),
                                   index=_directory_index(call))}


# LLM: 这里只留原生资产变更的耐久结果回执，不复制技能权威状态；原Store绑定owner/subject/key。
# 函数用途: 执行一次技能写入并支持完整参数冲突和无副作用重放。
def _skill_receipted(call, mutate):
    _require(call)
    digest = hashlib.sha256(json.dumps([call.action, call.params, call.expected_revision],
        ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    # LLM: 回执不保存正文或第二份技能数据，只保存经过原生读回的公开结果。
    # 函数用途: 在原Store事务持锁期间调用实际资产变更。
    def apply(records):
        _require(call)
        return mutate()

    return v13_store_for(call).transaction('skill_operations', apply,
        idempotency_key=call.idempotency_key, params_digest=digest)


# LLM: 读取只沿 no-follow 链，缺失返回 None 供创建路径区分新增与同键重放。
# 函数用途: 读取用户层某个 Skill 的当前正文。
def _existing_text(call, skill_id):
    raw = read_bytes_beneath(Path(call.owner_home), ('skills', skill_id, 'SKILL.md'),
                             max_bytes=4 * 1024 * 1024)
    return None if raw is None else raw.decode('utf-8')


# LLM: 同键完整参数由耐久回执绑定；目标不存在检查和写入在原生同锁内完成。
# 函数用途: 创建一个用户层 Skill。
def create_skill(call):
    _require(call)
    skill_id = _safe_skill_id(call.params['skill_id'])
    body = call.params['body_markdown']
    parsed = _parse_body(call, body, skill_id)
    if not parsed['valid']:
        raise BusinessError('INVALID_SCHEMA')
    return _skill_receipted(call, lambda: _write_skill(call, skill_id, body, creating=True))


# LLM: 只有用户层可改；同键先重放，CAS由原生资产锁内的当前正文决定，不在锁外比较。
# 函数用途: 按目标版本 CAS 更新用户层 Skill 正文。
def update_skill(call):
    _require(call)
    skill_id = _safe_skill_id(call.params['skill_id'])
    body = call.params['body_markdown']
    parsed = _parse_body(call, body, skill_id)
    if not parsed['valid']:
        raise BusinessError('INVALID_SCHEMA')
    return _skill_receipted(call, lambda: _write_skill(call, skill_id, body))


# LLM: 启停只沿原生侧元数据锁与 owner 配额，已在目标态时不重复写。
# 函数用途: 切换一个可管理 Skill 的启停状态。
def _toggle(call, enabled):
    from ..assets.skill_adapter import SkillAdapter

    _require(call)
    skill_id = _safe_skill_id(call.params['skill_id'])
    descriptor = _descriptor(call, skill_id)
    learned = _learned_names(call)
    if _layer(descriptor, learned) not in {'user', 'learned'}:
        raise BusinessError('STATE_CONFLICT')
    if bool(descriptor.get('enabled', True)) != enabled:
        try:
            _require(call)
            SkillAdapter(call.owner_home).set_enabled(descriptor['id'], enabled)
        except (ValueError, FileNotFoundError) as error:
            raise BusinessError('STATE_CONFLICT') from error
    learned = _learned_names(call)
    return {'skill': _view(_descriptor(call, skill_id), learned=learned, index=_directory_index(call))}


# LLM: 启用沿原生技能侧文件，重放返回旧回执而不覆写后来停用的当前状态。
# 函数用途: 启用一个用户层或自学 Skill。
def activate(call):
    return _skill_receipted(call, lambda: _toggle(call, True))


# LLM: 停用不删除正文与版本，真实变更与耐久结果回执使用同一幂等边界。
# 函数用途: 停用一个用户层或自学 Skill。
def retire(call):
    return _skill_receipted(call, lambda: _toggle(call, False))


# LLM: 回测只派发原生后台任务，HTTP 线程不执行模型；空间必须是真实记录。
# 函数用途: 为一个 Skill 创建在线回测作业。
def backtest(call):
    _require(call)
    descriptor = _descriptor(call, call.params['skill_id'])
    space_id = call.params.get('space_id')
    if not space_id:
        from ..gateway_parts.business_api_v13 import v13_call_frozen

        page = v13_call_frozen(call, 'spaces.list', {'limit': 1})
        space_id = page['items'][0]['space_id'] if page.get('items') else None
        if not space_id:
            raise BusinessError('INVALID_SCHEMA')
    body = descriptor.get('body_markdown') or ''
    cases = call.params.get('cases') or []
    instruction = ('对 Skill「' + descriptor['name'] + '」做在线回测。逐个执行给定用例，'
                   '最后只输出严格 JSON：{"skill_id": "' + descriptor['id'] + '", "cases_run": 整数, '
                   '"passed": 整数, "failed": 整数, "findings": ["每条不超过1000字"]}。\n'
                   '用例：' + json.dumps(cases, ensure_ascii=False, sort_keys=True) + '\n'
                   'Skill 正文：\n' + body[:20000])
    return v13_start_model_job(call, 'skill_backtest', instruction, space_id=space_id)


# LLM: 模型正文可能带代码围栏或解释文字，只取第一个完整 JSON 对象，不猜不补字段。
# 函数用途: 从模型输出中提取一个 JSON 对象。
def _json_object(text):
    if not isinstance(text, str):
        raise ValueError('模型输出不是文本')
    value = text.strip()
    if value.startswith('```'):
        lines = [line for line in value.splitlines() if not line.strip().startswith('```')]
        value = '\n'.join(lines).strip()
    try:
        parsed = json.loads(value)
    except ValueError:
        start, end = value.find('{'), value.rfind('}')
        if start < 0 or end <= start:
            raise
        parsed = json.loads(value[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError('模型输出不是对象')
    return parsed


# LLM: 回测结果只接受结构化整数与有界文本，任何缺字段都让作业明确失败。
# 函数用途: 把模型输出收口成 skill_backtest 结果结构。
def finalize_backtest(text):
    value = _json_object(text)
    skill_id = value.get('skill_id')
    if not isinstance(skill_id, str) or not skill_id.strip() or len(skill_id) > 128:
        raise ValueError('skill_id 无效')
    counts = []
    for key in ('cases_run', 'passed', 'failed'):
        item = value.get(key)
        if type(item) is not int or item < 0:
            raise ValueError(key + ' 必须是非负整数')
        counts.append(item)
    findings = value.get('findings')
    if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
        raise ValueError('findings 必须是字符串数组')
    return {'skill_id': skill_id.strip(), 'cases_run': counts[0], 'passed': counts[1],
            'failed': counts[2], 'findings': [item[:1000] for item in findings][:50]}


# LLM: 候选只投影原生账本；记忆候选来自 owner 候选库，Skill 候选来自原生提案目录。
# 函数用途: 返回当前 owner 的原生记忆候选服务。
def _memory_service(call):
    from ..memory_store.candidates import CandidateService

    return CandidateService(Path(_home_paths(call).owner_memory_candidates_jsonl))


# LLM: 缺失是正常空结果，损坏由原生服务明确抛出，不伪装成没有候选。
# 函数用途: 按编号读取一条记忆候选。
def _memory_get(call, candidate_id):
    try:
        return _memory_service(call).get(candidate_id)
    except KeyError:
        return None


# LLM: 原生候选状态不等于契约四态，映射只做展示投影，不改原状态机。
# 函数用途: 把原记忆候选状态映射成契约状态。
def _memory_status(value):
    mapping = {'observed': 'pending', 'pending_review': 'pending', 'approved': 'previewed',
               'promoted': 'confirmed', 'rejected': 'rejected'}
    if value in mapping:
        return mapping[value]
    return 'pending' if str(value).startswith('blocked_') else 'rejected'


# LLM: 标题与摘要只取候选正文的有界前缀，不回显内部证据或路径。
# 函数用途: 把原记忆候选投影成契约 LearningCandidate。
def _memory_row(candidate, now):
    content = str(getattr(candidate, 'content', '') or '')
    title = next((line.strip() for line in content.splitlines() if line.strip()), '记忆候选')[:300]
    created = str(getattr(candidate, 'created_at', '') or getattr(candidate, 'observed_at', '') or now)
    return {'candidate_id': candidate.candidate_id, 'kind': 'memory', 'title': title,
            'summary': content[:4000], 'status': _memory_status(candidate.status),
            'source_thread_id': None, 'created_at': created}


# LLM: 提案服务是原生自学习 Skill 的唯一候选权威，不另存草稿或状态。
# 函数用途: 返回当前 owner 的原生 Skill 提案服务。
def _proposal_service(call):
    from ..capability.skill_proposals import SkillProposalService

    return SkillProposalService(_home_paths(call))


# LLM: 提案 ID 只接受原生 24 位十六进制，缺失返回 404。
# 函数用途: 按编号读取一条原生 Skill 提案。
def _proposal_get(call, proposal_id):
    from ..capability.skill_proposals import SkillProposalError

    try:
        return _proposal_service(call).show(proposal_id)
    except SkillProposalError as error:
        if error.code == 'SKILL_PROPOSAL_NOT_FOUND':
            raise BusinessError('RESOURCE_NOT_FOUND') from error
        if error.code == 'SKILL_PROPOSAL_INVALID_ID':
            raise BusinessError('INVALID_SCHEMA') from error
        raise BusinessError('HOST_NOT_READY') from error


# LLM: 目标版本取整条提案记录的摘要，任一字段变化都会让旧预览失效。
# 函数用途: 计算 Skill 提案的 CAS 版本引用。
def _proposal_version(proposal):
    raw = json.dumps(proposal.to_record(), ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


# LLM: 提案状态是单向机，契约只投影待确认、已提交、已拒绝三态。
# 函数用途: 把原生提案状态映射成契约状态。
def _proposal_status(value):
    return {'pending_confirmation': 'pending', 'committed': 'confirmed',
            'rejected': 'rejected'}.get(value, 'pending')


# LLM: 摘要来自宿主按固定模板生成的草稿，不重新渲染或调用模型。
# 函数用途: 把原生 Skill 提案投影成契约 LearningCandidate。
def _proposal_row(proposal):
    title = ('经验 Skill 提案：' + proposal.target.skill_name)[:300]
    return {'candidate_id': proposal.proposal_id, 'kind': 'skill', 'title': title,
            'summary': str(proposal.draft.description or '')[:4000],
            'status': _proposal_status(proposal.status), 'source_thread_id': None,
            'created_at': proposal.created_at}


# LLM: 业务动作没有 IM 会话，确认窗口用稳定的空间线程键绑定本次用户与空间。
# 函数用途: 计算候选审核线程标识。
def _thread_id(call):
    space = call.params.get('space_id')
    return 'v13-space:' + str(space) if space else 'v13-owner:' + call.owner_id


# LLM: 身份只从可信home三要素重建；同键命令稳定重放，各子命令分别绑定，空间隔离会话。
# 函数用途: 构造原生候选控制使用的 Gateway 作用域。
def _control_scope(call, text):
    from ..gateway_parts.control_service import GatewayControlScope
    from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    paths = _home_paths(call)
    identity = OwnerIdentity(paths.owner_provider, paths.owner_kind, paths.owner_id.rsplit('/', 1)[-1])
    resolved = resolve_owner_home(paths.root, identity)
    if resolved.owner_id != call.owner_id or Path(resolved.home_dir).resolve() != Path(call.owner_home).resolve():
        raise BusinessError('HOST_NOT_READY')
    message = hashlib.sha256(json.dumps([call.owner_id, call.subject_id, call.action,
        call.idempotency_key, call.params, text], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return GatewayControlScope(str(call.subject_id), 'business', 'v13-learning:' + _thread_id(call),
        {'message_id': 'v13-' + message}, resolved_owner=identity)


# LLM: 原生预览回执是唯一 nonce 账，这里只走同一控制入口，不复制候选或确认状态。
# 函数用途: 执行一次原生 /memory candidates 控制命令并返回其结果。
def _memory_control(call, text):
    from contextlib import ExitStack

    from ..gateway_parts.control_operation_service import execute_gateway_control_operation
    from ..gateway_parts.memory_candidate_control import (
        memory_http_ingress,
        parse_memory_candidate_command,
    )
    from ..gateway_parts.paths import gateway_paths_from_root

    command = parse_memory_candidate_command(text)
    if not command.valid:
        raise BusinessError('INVALID_SCHEMA')
    paths = gateway_paths_from_root(Path(call.owner_home) / 'gateway')
    scope = _control_scope(call, text)
    background = getattr(call.services, 'background', None)
    if background is None:
        raise BusinessError('HOST_NOT_READY')
    try:
        with ExitStack() as stack:
            host = background._complete_host(call.context, stack) or call.context.host
            if getattr(host, 'memory_candidates', None) is None:
                raise BusinessError('HOST_NOT_READY')
            _require(call)
            with memory_http_ingress(scope, command, True) as issued:
                receipt = execute_gateway_control_operation(
                    host, paths, command, issued, command_text='/memory ' + command.value)
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        raise BusinessError('HOST_NOT_READY') from error
    value = getattr(receipt, 'result', None)
    if not isinstance(value, dict):
        raise BusinessError('HOST_NOT_READY')
    return dict(value)


# LLM: 原控制结果码只做稳定映射，未识别的失败统一按宿主未就绪，不冒充成功。
# 函数用途: 把原生候选控制错误码映射为业务错误。
def _memory_error(code):
    mapping = {'MEMORY_CANDIDATE_NOT_REVIEWABLE': 'STATE_CONFLICT',
               'MEMORY_PREVIEW_NOT_FULLY_DISPLAYABLE': 'STATE_CONFLICT',
               'MEMORY_CONTROL_USE_AGENTS': 'STATE_CONFLICT',
               'MEMORY_CONTROL_NOT_USER_REQUEST': 'BUSINESS_ACCESS_DENIED',
               'MEMORY_CANDIDATE_VERSION_CHANGED': 'REVISION_CONFLICT',
               'MEMORY_PREVIEW_EXPIRED': 'STATE_CONFLICT',
               'MEMORY_PREVIEW_BINDING_MISMATCH': 'BUSINESS_ACCESS_DENIED',
               'MEMORY_CONTROL_RESULT_UNKNOWN': 'OUTCOME_UNKNOWN'}
    return BusinessError(mapping.get(str(code), 'HOST_NOT_READY'))


# LLM: 预览令牌不落盘，只把本次主体、线程与目标版本绑进签名；任何一项变化都自然失效。
# 函数用途: 生成 Skill 提案预览的绑定签名。
def _skill_signature(call, proposal_id, issued):
    material = '\x1f'.join([call.owner_id, str(call.subject_id), _thread_id(call), proposal_id, str(issued)])
    return hmac.new(_PREVIEW_KEY, material.encode('utf-8'), hashlib.sha256).hexdigest()


# LLM: 版本签名盖在绑定签名与目标版本上，确认时先核绑定再核 CAS。
# 函数用途: 生成 Skill 提案预览的目标版本签名。
def _version_signature(bind_signature, version_ref):
    return hmac.new(_PREVIEW_KEY, (bind_signature + '\x1f' + version_ref).encode('utf-8'),
                    hashlib.sha256).hexdigest()


# LLM: 令牌按十分钟窗口稳定，重放同一请求得到同一预览编号，窗口外必须重新预览。
# 函数用途: 构造 Skill 提案的预览编号。
def _skill_preview_id(call, proposal_id, version_ref):
    issued = int(call.clock()) // 600 * 600
    bind = _skill_signature(call, proposal_id, issued)
    return 'spv1-' + str(issued).zfill(10) + '-' + proposal_id + bind[:16] + _version_signature(bind, version_ref)[:16]


# LLM: 令牌格式、绑定、时效和目标版本分开核验，失败原因保持结构化。
# 函数用途: 解析并校验 Skill 提案预览编号。
def _skill_preview_target(call, preview_id, expected_version_ref):
    match = _PREVIEW_RE.fullmatch(preview_id) if 'spv1-' in preview_id else None
    if match is None:
        raise BusinessError('RESOURCE_NOT_FOUND')
    issued = int(match.group(1))
    now = int(call.clock())
    if not issued <= now < issued + 600:
        raise BusinessError('STATE_CONFLICT')
    bind = _skill_signature(call, match.group(2), issued)
    if not hmac.compare_digest(bind[:16], match.group(3)):
        raise BusinessError('BUSINESS_ACCESS_DENIED')
    if not hmac.compare_digest(_version_signature(bind, expected_version_ref)[:16], match.group(4)):
        raise BusinessError('REVISION_CONFLICT')
    return match.group(2)


# LLM: 候选列表合并两个原生来源后统一排序与分页，不写第二份候选索引。
# 函数用途: 列出记忆与 Skill 自学习候选。
def candidates_list(call):
    _require(call)
    now = _now(call)
    kind = call.params.get('kind')
    rows = []
    if kind in (None, 'memory'):
        service = _memory_service(call)
        try:
            rows.extend(_memory_row(row, now) for row in service.list(limit=0))
        except (OSError, ValueError, RuntimeError) as error:
            raise BusinessError('HOST_NOT_READY') from error
    if kind in (None, 'skill'):
        from ..capability.skill_proposals import SkillProposalError

        try:
            rows.extend(_proposal_row(row) for row in _proposal_service(call).list())
        except SkillProposalError as error:
            raise BusinessError('HOST_NOT_READY') from error
    status = call.params.get('status')
    if status:
        rows = [row for row in rows if row['status'] == status]
    rows.sort(key=lambda row: (row['created_at'], row['candidate_id']), reverse=True)
    limit = call.params.get('limit')
    return _page(rows, [call.owner_id, call.action, kind, status, call.params.get('space_id')],
                 cursor=call.params.get('cursor'), limit=50 if limit is None else limit)


# LLM: 完整预览必须能返回用户将看到的全文和目标版本，不能只给摘要。
# 函数用途: 为一个候选生成完整预览并返回目标 CAS 版本。
def candidates_preview(call):
    _require(call)
    candidate_id = str(call.params['candidate_id'])
    now = _now(call)
    memory = _memory_get(call, candidate_id)
    if memory is not None:
        result = _memory_control(call, 'candidates preview ' + candidate_id)
        if not result.get('ok'):
            raise _memory_error(result.get('error_code'))
        details = result.get('details') or {}
        if details.get('state') != 'preview':
            raise BusinessError('STATE_CONFLICT')
        value = details.get('value') or {}
        return {'preview_id': details['nonce'], 'candidate': _memory_row(memory, now),
                'full_preview': json.dumps(value, ensure_ascii=False, sort_keys=True)[:200000],
                'target': {'kind': 'memory_candidate', 'candidate_id': candidate_id,
                           'promotion_target': value.get('promotion_target'),
                           'target_entry_id': value.get('target_entry_id')},
                'version_ref': details['version'],
                'expires_at': datetime.fromtimestamp(details['expires_at'], timezone.utc).isoformat()}
    proposal = _proposal_get(call, candidate_id)
    if proposal.status != 'pending_confirmation':
        raise BusinessError('STATE_CONFLICT')
    version = _proposal_version(proposal)
    issued = int(call.clock()) // 600 * 600
    return {'preview_id': _skill_preview_id(call, proposal.proposal_id, version),
            'candidate': _proposal_row(proposal),
            'full_preview': _render_proposal(proposal)[:200000],
            'target': {'kind': 'skill_proposal', 'skill_name': proposal.target.skill_name,
                       'before': proposal.target.before, 'revision': proposal.revision},
            'version_ref': version,
            'expires_at': datetime.fromtimestamp(issued + 600, timezone.utc).isoformat()}


# LLM: 预览正文使用原生固定模板渲染，和确认后真正安装的内容保持一致。
# 函数用途: 渲染一条 Skill 提案的完整 SKILL.md 文本。
def _render_proposal(proposal):
    from ..capability.skill_proposals import render_skill_markdown

    return render_skill_markdown(proposal.target.skill_name, proposal.draft)


# LLM: 确认必须核到原 owner/actor/thread、完整预览版本与目标 CAS 后才提交。
# 函数用途: 确认一条候选并返回候选终态和回执。
def candidates_confirm(call):
    _require(call)
    preview_id = str(call.params['preview_id'])
    expected = str(call.params['expected_version_ref'])
    now = _now(call)
    if preview_id.startswith('gwctl-msg-'):
        return _confirm_memory(call, preview_id, expected, now)
    proposal_id = _skill_preview_target(call, preview_id, expected)
    proposal = _proposal_get(call, proposal_id)
    if proposal.status == 'committed':
        return {'candidate': _proposal_row(proposal), 'receipt': dict(proposal.receipt)}
    if proposal.status != 'pending_confirmation':
        raise BusinessError('STATE_CONFLICT')
    if _proposal_version(proposal) != expected:
        raise BusinessError('REVISION_CONFLICT')
    outcome = _proposal_service(call).confirm(proposal.proposal_id, proposal.revision,
                                             actor=str(call.subject_id))
    if not outcome.ok:
        raise _proposal_error(outcome.code)
    return {'candidate': _proposal_row(outcome.proposal), 'receipt': dict(outcome.detail) | {'code': outcome.code}}


# LLM: 记忆候选确认先读原预览回执核对 CAS，再交给原生确认链，避免跨版本确认。
# 函数用途: 确认一条记忆候选。
def _confirm_memory(call, preview_id, expected, now):
    receipt = _memory_control(call, 'candidates receipt ' + preview_id)
    if not receipt.get('ok'):
        raise _memory_error(receipt.get('error_code'))
    details = receipt.get('details') or {}
    if details.get('version') != expected:
        raise BusinessError('REVISION_CONFLICT')
    if details.get('state') != 'preview':
        if details.get('state') != 'completed':
            raise BusinessError('STATE_CONFLICT')
        candidate = _memory_get(call, str(details.get('candidate_id') or ''))
        if candidate is None:
            raise BusinessError('RESOURCE_NOT_FOUND')
        return {'candidate': _memory_row(candidate, now),
                'receipt': {'state': details.get('state'), 'promotion': details.get('promotion') or {},
                            'candidate_version': details.get('version')}}
    result = _memory_control(call, 'candidates confirm ' + preview_id)
    final = result.get('details') or {}
    if not result.get('ok') and final.get('state') != 'completed':
        raise _memory_error(result.get('error_code'))
    candidate = _memory_get(call, str(final.get('candidate_id') or details.get('candidate_id') or ''))
    if candidate is None:
        raise BusinessError('RESOURCE_NOT_FOUND')
    return {'candidate': _memory_row(candidate, now),
            'receipt': {'state': final.get('state'), 'promotion': final.get('promotion') or {},
                        'candidate_version': details.get('version')}}


# LLM: 拒绝是终态；重复拒绝返回同一终态，不再改动候选或正文。
# 函数用途: 拒绝一条候选。
def candidates_reject(call):
    _require(call)
    preview_id = str(call.params['preview_id'])
    now = _now(call)
    if preview_id.startswith('gwctl-msg-'):
        result = _memory_control(call, 'candidates reject ' + preview_id)
        if not result.get('ok'):
            raise _memory_error(result.get('error_code'))
        details = result.get('details') or {}
        candidate = _memory_get(call, str(details.get('candidate_id') or ''))
        if candidate is None:
            raise BusinessError('RESOURCE_NOT_FOUND')
        return {'candidate': _memory_row(candidate, now)}
    match = _PREVIEW_RE.fullmatch(preview_id) if 'spv1-' in preview_id else None
    if match is None:
        raise BusinessError('RESOURCE_NOT_FOUND')
    issued = int(match.group(1))
    if not issued <= int(call.clock()) < issued + 600:
        raise BusinessError('STATE_CONFLICT')
    bind = _skill_signature(call, match.group(2), issued)
    if not hmac.compare_digest(bind[:16], match.group(3)):
        raise BusinessError('BUSINESS_ACCESS_DENIED')
    proposal = _proposal_get(call, match.group(2))
    if proposal.status == 'rejected':
        return {'candidate': _proposal_row(proposal)}
    if proposal.status != 'pending_confirmation':
        raise BusinessError('STATE_CONFLICT')
    outcome = _proposal_service(call).reject(proposal.proposal_id, proposal.revision)
    if not outcome.ok:
        raise _proposal_error(outcome.code)
    return {'candidate': _proposal_row(outcome.proposal or _proposal_get(call, proposal.proposal_id))}


# LLM: 原生提案结果码只做稳定映射，禁止把失败确认当成已安装。
# 函数用途: 把原生提案结果码映射为业务错误。
def _proposal_error(code):
    mapping = {'SKILL_PROPOSAL_NOT_FOUND': 'RESOURCE_NOT_FOUND',
               'SKILL_PROPOSAL_REVISION_MISMATCH': 'REVISION_CONFLICT',
               'SKILL_PROPOSAL_NOT_PENDING': 'STATE_CONFLICT',
               'SKILL_PROPOSAL_GUARD_BLOCKED': 'VERIFICATION_FAILED',
               'SKILL_PROPOSAL_TARGET_EXISTS': 'STATE_CONFLICT',
               'SKILL_PROPOSAL_STORAGE_LIMIT': 'LIMIT_EXCEEDED',
               'SKILL_PROPOSAL_SOURCE_MISSING': 'STATE_CONFLICT',
               'SKILL_PROPOSAL_SOURCE_CHANGED': 'STATE_CONFLICT',
               'SKILL_PROPOSAL_SOURCE_INACTIVE': 'STATE_CONFLICT'}
    return BusinessError(mapping.get(str(code), 'HOST_NOT_READY'))
