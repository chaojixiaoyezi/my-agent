# LLM: 目录覆盖不授执行或写权；批量沿原canonical/nofollow/锁，调度按单job恢复，已提交决定仅补历史，不建第二资产库。
# 模块用途: 合并项目、Owner和内置资产/规则，提供带CAS与持久化回滚的两阶段批量保存和晋升。
import base64
import json
import os
import re
import stat
import subprocess
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import fields, replace
from pathlib import Path

from ..capability.skills import split_skill_frontmatter
from ..common.json_io import _flock_descriptor, _funlock_descriptor, locked_json_path
from ..common.nofollow_fs import (
    NoFollowPathError,
    _supports_dir_fd,
    list_names_beneath,
    open_directory_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_bytes_atomic_beneath,
    write_text_atomic_beneath,
)
from . import (
    MAX_ASSET_BYTES,
    _open_readonly_lock_beneath,
    _verify_directory_beneath,
    asset_root_lock,
    next_asset_revision,
    safe_asset_id,
    skill_asset_id,
    workspace_asset_home,
)
from .descriptor import ASSET_KINDS, AssetDescriptor

_BATCH_FILE = '.asset-batch.json'
_BATCH_LIMIT = MAX_ASSET_BYTES * 16
_DESCRIPTOR_FIELDS = {item.name for item in fields(AssetDescriptor)} | {'schema_version'}


# LLM: 所有叶均经原nofollow且有界读取，缺失与损坏分别处理，不创建目录。
# 函数用途: 取得受信资产根内的一份文本或JSON对象。
def _text(root, parts, *, limit=MAX_ASSET_BYTES):
    raw = read_bytes_beneath(root, parts, max_bytes=limit)
    return None if raw is None else raw.decode('utf-8')


# LLM: JSON结构不宽松修复，不让损坏资产变成空集合或触发低优先级回退。
# 函数用途: 安全读取可缺失的JSON对象。
def _object(root, parts):
    text = _text(root, parts)
    value = json.loads(text) if text is not None else None
    if value is not None and not isinstance(value, dict):
        raise ValueError('资产元数据必须是JSON对象')
    return value


# LLM: 附件与undo保持原始字节；UTF8仍用原文本格式，二进制只在传输和undo内以显式base64包装，不建旁路副本。
# 函数用途: 冻结一个资产叶并生成可JSON保存的无损内容。
def _file_content(root, parts):
    raw = read_bytes_beneath(root, parts, max_bytes=MAX_ASSET_BYTES)
    if raw is None:
        return None
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return {'data_base64': base64.b64encode(raw).decode('ascii')}


# LLM: 二进制包装仅允许一个明确字段，先限编码体积再严格解码；不接受路径或可执行对象。
# 函数用途: 校验草稿资源或恢复记录的文本/二进制内容。
def _content_bytes(content):
    if content is None:
        return None
    if isinstance(content, str):
        return content.encode('utf-8')
    if (not isinstance(content, dict) or set(content) != {'data_base64'}
            or not isinstance(content['data_base64'], str) or len(content['data_base64']) > MAX_ASSET_BYTES * 2):
        raise ValueError('资产内容必须是文本或data_base64对象')
    return base64.b64decode(content['data_base64'], validate=True)


# LLM: 只打开已有锁，读取前后比同名inode；无锁期间出生锁亦失败关闭，绝不创建或收紧权限。
# 函数用途: 为冷读取得根事务锁或技能/监控的既有锁。
@contextmanager
def _read_lock(root, parts):
    descriptor = None
    try:
        descriptor = _open_readonly_lock_beneath(root, parts)
    except FileNotFoundError:
        pass
    try:
        if descriptor is not None:
            _flock_descriptor(descriptor)
        _check_lock(root, parts, descriptor)
        try:
            yield
        finally:
            _check_lock(root, parts, descriptor)
    finally:
        if descriptor is not None:
            _funlock_descriptor(descriptor)
            os.close(descriptor)


# LLM: 锁的存在与身份是机械观察，不能凭文件名或正文把未持有的锁当成一致快照。
# 函数用途: 核对只读锁尚为同一单链接普通文件。
def _check_lock(root, parts, descriptor):
    try:
        current = _open_readonly_lock_beneath(root, parts)
    except FileNotFoundError:
        if descriptor is not None:
            raise BlockingIOError('资产锁已消失，请重试') from None
        return
    try:
        if descriptor is None:
            raise BlockingIOError('资产锁已出现，请重试')
        before, after = os.fstat(descriptor), os.fstat(current)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise BlockingIOError('资产锁已变化，请重试')
    finally:
        os.close(current)


# LLM: 内部视图不运行Store构造器，避免mkdir；领域解码与已有Store相同，所有写口保持禁止。
# 函数用途: 为明确来源构造无IO的脚本或工作流只读视图。
def _store_view(root, kind):
    from .script_store import ScriptStore
    from .workflow_store import WorkflowStore

    cls = ScriptStore if kind == 'script' else WorkflowStore
    view = object.__new__(cls)
    view.owner_home, view.collection, view.filename = root, kind + 's', kind + '.json'
    view._read_only, view.is_admin, view._global_roots, view.workspace_root = True, False, (), None
    return view


# LLM: 工作流外层仍用原定义schema，公共资产清单用平铺描述符；转换不产生权限或写盘。
# 函数用途: 将平铺的工作流详情恢复为原WorkflowDefinition。
def _workflow_definition(record):
    from .workflow_store import WorkflowDefinition

    return WorkflowDefinition.from_any({'descriptor': {key: value for key, value in record.items()
        if key in _DESCRIPTOR_FIELDS}, 'steps': record['steps'], 'body_markdown': record.get('body_markdown', '')})


# LLM: 来源事实由遍历根决定，覆盖任何正文伪造的作用域；scope_layer全局与source_scope内置分别展示。
# 函数用途: 给一份独立资产字典标记实际来源。
def _project(record, layer, workspace):
    return {**record, 'scope_layer': 'global' if layer == 'builtin' else layer,
            'source_scope': layer, 'workspace_root': str(workspace) if layer == 'workspace' else ''}


# LLM: 技能路径来自已发现目录，不用asset_id猜路径；正文/扩展复用现有codec，额外元数据仅展示。
# 函数用途: 读取一个已发现技能并保留CAS与晋升出处。
def _skill_record(path, *, include_body, source='owner'):
    from .skill_adapter import _catalog_descriptor, _sidecar

    record = _catalog_descriptor(path, source, '', include_body)
    sidecar = _sidecar(path.parent)
    record['metadata'] = {**sidecar.get('metadata', {}), **record['metadata']}
    record['revision'] = sidecar.get('revision', 0)
    AssetDescriptor.from_dict({key: value for key, value in record.items() if key in _DESCRIPTOR_FIELDS})
    return record


# LLM: 监控仍读canonical data/monitors绑定和Scheduler事实；workspace仅为模板，不注册或启动定时任务。
# 函数用途: 取得监控模板或Owner监控的描述符，保留版本及出处。
def _monitor_record(root, identifier, layer):
    from .monitor_adapter import _jobs, get_monitor_descriptor

    parts = ('monitors', identifier + '.json') if layer == 'workspace' else ('data', 'monitors', identifier + '.json')
    binding = _object(root, parts)
    if layer == 'workspace':
        if binding is None:
            return None
        descriptor = AssetDescriptor.from_dict({key: value for key, value in binding.items() if key in _DESCRIPTOR_FIELDS})
        if descriptor.id != identifier or descriptor.kind != 'monitor':
            raise ValueError('监控模板身份不匹配')
        return descriptor.to_dict() | {'schedule_spec': binding.get('schedule_spec', {})}
    job = next((row for row in _jobs(root) if row.get('job_id') == identifier), None)
    if job is not None and job.get('status') == 'deleted':
        return None
    try:
        record = get_monitor_descriptor(root, identifier, job)
    except FileNotFoundError:
        return None
    if binding:
        record['revision'] = binding.get('revision', 0)
        record['metadata'] = {**binding.get('metadata', {}), **record['metadata']}
    return record


# LLM: 覆盖只投影调用方授权的根，冷读不建.agent、锁或Owner；快照授权由调用方另行传给原Skill服务。
# 类用途: 提供四类资产与规则的workspace > owner > builtin只读发现入口。
class WorkspaceAssetOverlay:
    # LLM: 不resolve内部链接、不创建根；builtin_roots是资产家目录序列，不是权限或自动共享来源。
    # 函数用途: 绑定项目、Owner和显式内置资产根。
    def __init__(self, workspace_root=None, *, owner_home=None, builtin_roots=()):
        self.workspace_root = Path(workspace_root).absolute() if workspace_root is not None else None
        self.owner_home = Path(owner_home).absolute() if owner_home is not None else None
        self.builtin_roots = tuple(Path(root).absolute() for root in builtin_roots)

    # LLM: 项目.agent逐段核验；来源顺序唯一，不从描述符source_scope或metadata恢复路径。
    # 函数用途: 枚举当前视图的准确根与优先级。
    def _layers(self):
        if self.workspace_root is not None:
            yield workspace_asset_home(self.workspace_root), 'workspace'
        if self.owner_home is not None:
            yield self.owner_home, 'owner'
        for root in self.builtin_roots:
            yield root, 'builtin'

    # LLM: 根既有锁包住本来源读取，未提交批量不隐式修复；损坏高优先级资产明确失败，不执行正文。
    # 函数用途: 依准确kind/id取得一份有效资产详情，缺失返回None。
    def get_asset(self, asset_kind, asset_id):
        if asset_kind not in ASSET_KINDS:
            raise ValueError('未知资产kind')
        safe_asset_id(asset_id)
        for root, layer in self._layers():
            with _read_lock(root, ('.asset-batch.lock',)):
                self._check_batch(root)
                records = self._records(root, layer, asset_kind, requested=asset_id)
                if records:
                    return _project(records[0], layer, self.workspace_root)
        return None

    # LLM: 同ID只读取首来源，避免低优先级坏记录击穿覆盖；列表不加载脚本代码，不将发现等同启用。
    # 函数用途: 列出单类或四类去重资产描述符，排序稳定且无写盘副作用。
    def list_assets(self, asset_kind=None):
        kinds = (asset_kind,) if asset_kind is not None else ASSET_KINDS
        if any(kind not in ASSET_KINDS for kind in kinds):
            raise ValueError('未知资产kind')
        found = {}
        for root, layer in self._layers():
            with _read_lock(root, ('.asset-batch.lock',)):
                self._check_batch(root)
                for kind in kinds:
                    excluded = {identifier for key_kind, identifier in found if key_kind == kind}
                    for row in self._records(root, layer, kind, excluded=excluded):
                        found.setdefault((kind, row['id']), _project(row, layer, self.workspace_root))
        return [found[key] for key in sorted(found)]

    # LLM: 此入口绝不把读取变成恢复写；协调记录不是第二套canonical资产存储。
    # 函数用途: 拒绝进程中断留下的未提交批量。
    def _check_batch(self, root):
        if _text(root, (_BATCH_FILE,), limit=_BATCH_LIMIT) is not None:
            raise ValueError('资产批量事务未提交，请显式写操作恢复')

    # LLM: 脚本/工作流复用原Store严格解码及集合锁，skill/monitor复用原适配器和格式。
    # 函数用途: 从一个准确来源枚举资产，不返回被遮蔽的ID。
    def _records(self, root, layer, kind, *, requested=None, excluded=()):
        if kind in {'script', 'workflow'}:
            view = _store_view(root, kind)
            with view._locked():
                values = ({requested: view._get(requested)} if requested is not None else view._local_records(excluded))
                return [value if kind == 'script' else value.descriptor.to_dict() | {
                    'steps': [step.to_dict() for step in value.steps], 'body_markdown': value.body_markdown}
                    for value in values.values() if value is not None]
        return _other_records(root, layer, kind, requested, excluded)

    # LLM: 规则合并按低到高顺序保留全部正文，项目根AGENTS及.agent/AGENTS均参与；正文不是文件权限。
    # 函数用途: 返回规则来源、有界正文与路径，供会话人格快照入口显式使用。
    def list_agents(self):
        sources = [(root, ('AGENTS.md',), 'builtin') for root in reversed(self.builtin_roots)]
        if self.owner_home is not None:
            sources.append((self.owner_home, ('AGENTS.md',), 'owner'))
        if self.workspace_root is not None:
            sources.extend((self.workspace_root, parts, 'workspace') for parts in
                           (('AGENTS.md',), ('.agent', 'AGENTS.md')))
        return [{'source_scope': layer, 'scope_layer': 'global' if layer == 'builtin' else layer,
                 'workspace_root': str(self.workspace_root) if layer == 'workspace' else '',
                 'path': str(root.joinpath(*parts)), 'content': text}
                for root, parts, layer in sources if (text := _text(root, parts)) is not None]


# LLM: 技能与监控只用真实发现位置；先按目录身份排除遮蔽技能再解码正文，拒绝任意内部链接，不建锁或目录。
# 函数用途: 枚举一个来源的技能或监控描述符，排除高优先级已认领的ID。
def _other_records(root, layer, kind, requested, excluded):
    if kind == 'skill':
        rows = []
        for path in _strict_skill_paths(root):
            identifier = skill_asset_id(path.parent.name)
            if identifier in excluded:
                continue
            with _read_lock(path.parent, ('skill_meta.json.lock',)):
                row = _skill_record(path, include_body=requested is not None, source=layer)
            if row['id'] not in excluded and (requested is None or row['id'] == requested):
                rows.append(row)
        return rows
    from .monitor_adapter import _jobs

    parent = ('monitors',) if layer == 'workspace' else ('data', 'monitors')
    identifiers = {safe_asset_id(name[:-5]) for name in list_names_beneath(root, parent) if name.endswith('.json')}
    if layer != 'workspace':
        identifiers.update(job['job_id'] for job in _jobs(root) if job.get('status') != 'deleted')
    identifiers = {requested} if requested is not None else identifiers - set(excluded)
    rows = []
    for identifier in sorted(identifiers):
        safe_asset_id(identifier)
        with _read_lock(root, (*parent, identifier + '.json.lock')):
            record = _monitor_record(root, identifier, layer)
        if record is not None:
            rows.append(record)
    return rows


# LLM: 逐段no-follow遍历技能分类，普通说明文件忽略但链接明确失败；最大深度与原Skill发现口一致，不创建缓存。
# 函数用途: 安全发现SKILL.md，允许Owner的learned及分类子目录。
def _strict_skill_paths(root):
    pending = [('skills',)]
    while pending:
        parts = pending.pop()
        names = list_names_beneath(root, parts)
        if 'SKILL.md' in names:
            yield root.joinpath(*parts, 'SKILL.md')
            continue
        if len(parts) >= 7 or not names:
            continue
        if not _supports_dir_fd():
            base = _verify_directory_beneath(root, parts)
            for name in reversed(names):
                if name.startswith('.'):
                    continue
                metadata = (base / name).lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise NoFollowPathError('技能目录不能包含符号链接')
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append((*parts, name))
            continue
        directory = open_directory_beneath(root, parts)
        try:
            for name in reversed(names):
                if name.startswith('.'):
                    continue
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise NoFollowPathError('技能目录不能包含符号链接')
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append((*parts, name))
        finally:
            os.close(directory)


# LLM: 只编译或调用固定shell的语法检查模式，不执行待保存代码，也不运行输入声明的任意解释器路径。
# 函数用途: 在批量暂存前验证Python或Shell入口语法。
def _validate_code(code, options):
    if not isinstance(code, str) or len(code.encode('utf-8')) > MAX_ASSET_BYTES:
        raise ValueError('脚本代码无效或超过安全预算')
    interpreter = Path(options['interpreter']).name
    if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', interpreter):
        compile(code, options['entrypoint'], 'exec', dont_inherit=True)
    elif interpreter in {'sh', 'bash'}:
        checked = subprocess.run(['/bin/' + interpreter, '-n'], input=code, text=True, capture_output=True, timeout=5)
        if checked.returncode:
            raise ValueError('脚本Shell语法无效')
    else:
        raise ValueError('该脚本解释器尚无安全语法校验器')


# LLM: 本入口只接受描述符加各kind既有负载，未知字段拒绝；平铺描述符仅为同一schema的传输形式。
# 函数用途: 拆出严格描述符、CAS前提和负载字段，不执行IO。
def _split_item(item):
    if not isinstance(item, dict):
        raise ValueError('批量项必须是对象')
    values = dict(item)
    expected = values.pop('expected_revision', None)
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ValueError('expected_revision必须是非负整数')
    descriptor = values.pop('descriptor', None)
    if descriptor is None:
        descriptor = {key: values.pop(key) for key in tuple(values) if key in _DESCRIPTOR_FIELDS}
    descriptor = AssetDescriptor.from_dict(descriptor.to_dict() if isinstance(descriptor, AssetDescriptor) else descriptor)
    if 'kind' in values and values.pop('kind') != descriptor.kind:
        raise ValueError('批量kind与描述符不一致')
    if 'id' in values and values.pop('id') != descriptor.id:
        raise ValueError('批量id与描述符不一致')
    return descriptor, values, expected


# LLM: 技能正文沿唯一frontmatter codec，资源只接受安全相对叶及文本/显式base64，不能覆盖SKILL或元数据。
# 函数用途: 验证技能正文、头部和可选配套资源，生成canonical文件集合。
def _skill_files(descriptor, values):
    if values.keys() - {'body_markdown', 'files'}:
        raise ValueError('技能批量项包含未知字段')
    body = values.get('body_markdown')
    if not isinstance(body, str) or not body.strip():
        raise ValueError('技能正文不能为空')
    header, _ = split_skill_frontmatter(body)
    if not isinstance(header.get('name'), str) or not header['name'].strip():
        raise ValueError('技能frontmatter必须包含name')
    from .skill_adapter import _descriptor

    _descriptor(Path(descriptor.id), {**header, **descriptor.to_dict()}, descriptor.owner_id)
    result = {('skills', descriptor.id, 'SKILL.md'): body,
              ('skills', descriptor.id, 'skill_meta.json'): _json(descriptor.to_dict())}
    resources = values.get('files', {})
    if not isinstance(resources, dict):
        raise ValueError('技能files必须为相对路径到文本的对象')
    for name, content in resources.items():
        parts = _resource_parts(name)
        if len(parts) == 1 and parts[0].casefold() in {'skill.md', 'skill_meta.json'}:
            raise ValueError('技能资源不能覆盖权威正文或元数据')
        if content is None:
            raise ValueError('技能资源不能为空内容对象')
        _content_bytes(content)
        result[('skills', descriptor.id, *parts)] = content
    return result


# LLM: 资源路径不是任意宿主路径，逐段安全ID且禁锁/事务叶；用于写入和晋升同一格式。
# 函数用途: 校验技能资源的有限深度相对路径。
def _resource_parts(name):
    if not isinstance(name, str) or '\\' in name:
        raise ValueError('技能资源路径无效')
    parts = tuple(name.split('/'))
    if len(parts) > 8:
        raise ValueError('技能资源路径过深')
    for part in parts:
        safe_asset_id(part)
        if part.endswith('.lock') or part == 'asset-transaction.json':
            raise ValueError('技能资源不能使用锁或事务文件名')
    return parts


# LLM: 定时模板验证沿Scheduler唯一schema，省略every anchor时固定0仅表示模板，不注册或启动任务。
# 函数用途: 验证并规范监控调度声明，保留显式enabled展示值。
def _validate_schedule(spec):
    from ..scheduler.schedule import validate_schedule

    if not isinstance(spec, dict):
        raise ValueError('监控schedule_spec必须是对象')
    enabled = spec.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('监控enabled必须是布尔值')
    schedule = dict(spec)
    if schedule.get('kind') == 'every':
        schedule.setdefault('anchor_at', 0)
    return validate_schedule(schedule) | {'enabled': enabled}


# LLM: 四种负载复用既有定义schema和路径；本阶段仅构造内存计划，不创建根、锁或正式资产。
# 函数用途: 完成单项描述符、语法及schema预检并生成canonical写入计划。
def _prepare_item(item, scope, workspace):
    from .script_store import _script_options
    from .workflow_store import WorkflowDefinition, with_workflow_step_refs

    descriptor, values, expected = _split_item(item)
    descriptor = replace(descriptor, scope_layer=scope, source_scope=scope,
                         workspace_root=str(workspace) if scope == 'workspace' else '')
    kind, identifier = descriptor.kind, descriptor.id
    if kind == 'script':
        if values.keys() - {'code', 'entrypoint', 'interpreter', 'timeout_seconds'}:
            raise ValueError('脚本批量项包含未知字段')
        options = _script_options(values.get('entrypoint', 'main.py'), values.get('interpreter', 'python3'),
                                  values.get('timeout_seconds', 120))
        _validate_code(values.get('code'), options)
        payload = descriptor.to_dict() | options
        updates = {('scripts', identifier, options['entrypoint']): values['code']}
        metadata = ('scripts', identifier, 'script.json')
    elif kind == 'workflow':
        definition = with_workflow_step_refs(WorkflowDefinition.from_any({'descriptor': descriptor.to_dict(), **values}))
        descriptor = definition.descriptor
        payload = descriptor.to_dict() | {'steps': [step.to_dict() for step in definition.steps],
                                          'body_markdown': definition.body_markdown}
        updates = {('workflows', identifier, 'WORKFLOW.md'): definition.body_markdown or None}
        metadata = ('workflows', identifier, 'workflow.json')
    elif kind == 'skill':
        updates = _skill_files(descriptor, values)
        metadata, payload = ('skills', identifier, 'skill_meta.json'), descriptor.to_dict()
    else:
        if values.keys() - {'schedule_spec'}:
            raise ValueError('监控批量项包含未知字段')
        payload = descriptor.to_dict() | {'schedule_spec': _validate_schedule(values.get('schedule_spec', {}))}
        metadata = (('monitors', identifier + '.json') if scope == 'workspace'
                    else ('data', 'monitors', identifier + '.json'))
        updates = {}
    updates[metadata] = _json(payload)
    return {'descriptor': descriptor, 'metadata': metadata, 'payload': payload, 'updates': updates, 'expected': expected}


# LLM: 与原Store序列化一致，禁止NaN且按UTF8预算校验；不会写盘或自动脱敏代码。
# 函数用途: 编码canonical资产JSON。
def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


# LLM: 描述符批量共享安全ID，在大小写不敏感文件系统也不允许同一事务认领相同目标。
# 函数用途: 验证整个草稿包并检测重复身份或目标路径。
def _prepare_batch(items, scope, workspace):
    if not isinstance(items, list):
        raise ValueError('items必须是对象列表')
    plans = [_prepare_item(item, scope, workspace) for item in items]
    keys = [(plan['descriptor'].kind, plan['descriptor'].id.casefold()) for plan in plans]
    if len(set(keys)) != len(keys):
        raise ValueError('批量资产身份重复')
    paths = [tuple(part.casefold() for part in parts) for plan in plans for parts in plan['updates']]
    if len(set(paths)) != len(paths):
        raise ValueError('批量资产目标重复')
    for plan in plans:
        for parts, content in plan['updates'].items():
            _validate_batch_path(parts)
            if content is not None and len(_content_bytes(content)) > MAX_ASSET_BYTES:
                raise ValueError('资产文件超过安全预算')
    return plans


# LLM: journal文件undo只接受四类canonical资产；Scheduler必须用单job变更，禁止原整store快照回滚覆盖正常tick。
# 函数用途: 校验计划与恢复记录中的资产相对路径。
def _validate_batch_path(parts):
    if not isinstance(parts, (tuple, list)) or not parts:
        raise ValueError('批量恢复路径无效')
    if parts[0] in {'scripts', 'workflows', 'skills'} and len(parts) >= 3:
        safe_asset_id(parts[1])
        if parts[0] != 'skills' and len(parts) != 3:
            raise ValueError('资产路径深度无效')
        _resource_parts('/'.join(parts[2:]))
    elif len(parts) == 2 and parts[0] == 'monitors' or len(parts) == 3 and tuple(parts[:2]) == ('data', 'monitors'):
        if not parts[-1].endswith('.json'):
            raise ValueError('监控元数据路径无效')
        safe_asset_id(parts[-1][:-5])
    else:
        raise ValueError('批量恢复目标不属于canonical资产')


# LLM: root锁后按词法顺序取得原集合/单项锁，Scheduler叶与原Repository共用store.json.lock，禁止另取可重入锁。
# 函数用途: 为批量发表或恢复建立和普通Store/适配器相同的写锁集合。
@contextmanager
def _canonical_locks(root, paths):
    targets = set()
    for parts in paths:
        if tuple(parts) == ('data', 'scheduler', 'store.json'):
            targets.add(tuple(parts))
            continue
        _validate_batch_path(parts)
        if parts[0] in {'scripts', 'workflows'}:
            targets.add((parts[0], '.assets'))
        elif parts[0] == 'skills':
            targets.update({('skills', '.assets'), ('skills', parts[1], 'skill_meta.json')})
        else:
            targets.update({(*parts[:-1], '.assets'), tuple(parts)})
    with ExitStack() as stack:
        for parts in sorted(targets):
            _verify_directory_beneath(root, parts[:-1], create=True)
            stack.enter_context(locked_json_path(root.joinpath(*parts)))
            _verify_directory_beneath(root, parts[:-1])
        yield


# LLM: 调用方已持有原根/集合锁；复用脚本历史规则和入口别名，不读取投影路径。
# 函数用途: 合并脚本旧历史并规划过时入口叶的清理。
def _inherit_script(plan, old):
    from .script_store import _preserve_history, _record_descriptor

    descriptor = _preserve_history(plan['descriptor'], _record_descriptor(old))
    old_entry, entry = old['entrypoint'], plan['payload']['entrypoint']
    if old_entry.casefold() == entry.casefold():
        code = plan['updates'].pop(('scripts', descriptor.id, entry))
        plan['updates'][('scripts', descriptor.id, old_entry)] = code
        plan['payload']['entrypoint'] = old_entry
    elif old_entry != entry:
        plan['updates'][('scripts', descriptor.id, old_entry)] = None
    return descriptor


# LLM: 原Store旧undo先在同锁恢复再检查目标版本；技能和监控按已有schema验证，所有路径来自本计划。
# 函数用途: 读取并校验实际目标资产，为CAS取得正确的旧版本。
def _existing_target(root, plan):
    descriptor = plan['descriptor']
    kind, identifier = descriptor.kind, descriptor.id
    if kind in {'script', 'workflow'}:
        view = _store_view(root, kind)
        view._read_only = False
        old = view._get(identifier)
        if kind == 'script' and old is not None:
            plan['descriptor'] = _inherit_script(plan, old)
    elif kind == 'skill' and _text(root, ('skills', identifier, 'SKILL.md')) is not None:
        _skill_record(root / 'skills' / identifier / 'SKILL.md', include_body=False)
    previous = _object(root, plan['metadata'])
    if previous is not None and previous.get('id', identifier) != identifier:
        raise ValueError('已有资产身份不匹配')
    if kind == 'monitor' and previous is not None:
        AssetDescriptor.from_dict({key: value for key, value in previous.items() if key in _DESCRIPTOR_FIELDS})
    return previous


# LLM: CAS和旧记录解码必须在同一根锁、集合锁内；最终历史合并后的JSON仍检查可读字节预算。
# 函数用途: 冻结下一版元数据及所有最终更新叶，元数据保持最后发表。
def _finalize_plans(root, plans):
    for plan in plans:
        previous = _existing_target(root, plan)
        revision = next_asset_revision(previous, plan['expected'])
        descriptor, payload = replace(plan['descriptor'], revision=revision), plan['payload']
        payload.update(descriptor.to_dict())
        plan['descriptor'] = descriptor
        plan['updates'].pop(plan['metadata'], None)
        plan['updates'][plan['metadata']] = _json(payload)
        if any(content is not None and len(_content_bytes(content)) > MAX_ASSET_BYTES for content in plan['updates'].values()):
            raise ValueError('最终资产内容超过安全字节预算')


# LLM: 先快照全部目标叶，包含严格单链接检查，再出生任何暂存或正式内容；stage名只由宿主随机生成。
# 函数用途: 生成完整undo记录及有限的阶段一暂存计划。
def _batch_entries(root, plans):
    token, entries = uuid.uuid4().hex, []
    for plan in plans:
        for parts, content in plan['updates'].items():
            previous = _file_content(root, parts)
            entries.append({'parts': list(parts), 'previous': previous,
                            'stage': '.' + parts[-1] + '.stage-' + token, 'content': content})
    journal = {'schema_version': 'asset_batch.v1', 'entries': [
        {key: value for key, value in entry.items() if key != 'content'} for entry in entries]}
    if len(_json(journal).encode('utf-8')) > _BATCH_LIMIT:
        raise ValueError('批量恢复记录超过安全预算')
    return entries, journal


# LLM: 即使目标为删除也先nofollow读当前叶，防止共享unlink原语删除硬链接；替换前再次核验同一安全形状。
# 函数用途: 在已持有业务锁时无损保存或清除一个canonical文本/资源叶。
def _put_text(root, parts, content):
    read_bytes_beneath(root, parts, max_bytes=MAX_ASSET_BYTES)
    if content is None:
        unlink_file_beneath(root, parts)
    elif isinstance(content, dict):
        write_bytes_atomic_beneath(root, parts, _content_bytes(content))
    else:
        write_text_atomic_beneath(root, parts, content)


# LLM: 先undo再发表资产/job/due，v2耐久提交决定后只补原历史，不回滚已提交任务；任何收口失败均保留日志。
# 函数用途: 执行两阶段批量提交，成功时删除暂存和undo才返回。
def _commit_staged(root, entries, journal, *, admission=None):
    from .script_store_support import apply_scheduler_batch

    _decode_journal(_json(journal))
    write_text_atomic_beneath(root, (_BATCH_FILE,), _json(journal))
    try:
        for entry in entries:
            if entry['content'] is not None:
                _put_text(root, (*entry['parts'][:-1], entry['stage']), entry['content'])
        for entry in entries:
            parts = tuple(entry['parts'])
            if entry['content'] is not None:
                staged = _file_content(root, (*parts[:-1], entry['stage']))
                if _content_bytes(staged) != _content_bytes(entry['content']):
                    raise ValueError('资产暂存内容已变化')
            _put_text(root, parts, entry['content'])
        apply_scheduler_batch(root, journal.get('scheduler_jobs', {}), journal.get('scheduler_runs', {}),
            admission=admission, history=journal.get('scheduler_history', ()))
        if journal['schema_version'] == 'asset_batch.v2':
            journal = {**journal, 'committed': True}
            write_text_atomic_beneath(root, (_BATCH_FILE,), _json(journal))
        _finish_batch(root, journal, admission=admission)
    except BaseException:
        current = _text(root, (_BATCH_FILE,), limit=_BATCH_LIMIT)
        decided = _decode_journal(current) if current is not None else journal
        if not decided.get('committed'):
            _restore_batch(root, journal['entries'], journal.get('scheduler_jobs'), journal.get('scheduler_runs'))
        raise


# LLM: 已提交决定不可undo；原history去重后追加，失败保留同一journal供下次修复，不引入第二恢复账本。
# 函数用途: 幂等补齐已提交批量的取消历史，再清阶段文件和事务记录。
def _finish_batch(root, journal, *, admission=None):
    from ..common.json_io import append_private_jsonl_records
    from ..user_space.owner_quota import OwnerQuotaChange
    from .script_store_support import scheduler_batch_repository

    records = journal.get('scheduler_history', [])
    if records:
        repo = scheduler_batch_repository(root)
        history = repo._new_history_rows_unlocked(records)
        if admission is not None and history:
            size = len(''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in history).encode())
            admission.check([OwnerQuotaChange(repo.history_path, size, append=True)])
        if history:
            append_private_jsonl_records(repo.history_path, history)
    _clean_stages(root, journal['entries'])
    unlink_file_beneath(root, (_BATCH_FILE,))


# LLM: 仅清理本事务记录中的安全暂存叶，既有文件也须为单链接普通文件；不递归删除用户资源。
# 函数用途: 清除已提交或回滚事务的暂存文件。
def _clean_stages(root, entries):
    for entry in entries:
        parts = (*entry['parts'][:-1], entry['stage'])
        read_bytes_beneath(root, parts, max_bytes=MAX_ASSET_BYTES)
        unlink_file_beneath(root, parts)


# LLM: 持root/canonical全锁恢复旧资产及精确job/run，保留其它调度写入，store及due修复成功后才清undo。
# 函数用途: 在同一根锁和canonical锁集合内恢复失败批量。
def _restore_batch(root, entries, scheduler_jobs=None, scheduler_runs=None):
    from .script_store_support import apply_scheduler_batch

    for entry in reversed(entries):
        _put_text(root, tuple(entry['parts']), entry['previous'])
    apply_scheduler_batch(root, scheduler_jobs or {}, scheduler_runs or {}, previous=True)
    _clean_stages(root, entries)
    unlink_file_beneath(root, (_BATCH_FILE,))


# LLM: 已持root/store锁的已提交恢复调用；沿共享规范根构造器打开原Scheduler，不信journal自报Owner，复核后同步due。
# 函数用途: 将恢复后的Owner调度权威重新投影到原共享DueIndex。
def _sync_batch_scheduler(root):
    from .script_store_support import scheduler_batch_repository

    repo = scheduler_batch_repository(root)
    repo._sync_due_index_unlocked(repo._load_store_unlocked())


# LLM: v1仅恢复原资产叶；v2显式增加单job/run前后事实，旧整库undo失败关闭，完整结构核验后才恢复。
# 函数用途: 解码一个可由进程重启继续恢复的批量undo记录。
def _decode_journal(text):
    journal = json.loads(text)
    if not isinstance(journal, dict) or journal.get('schema_version') not in {'asset_batch.v1', 'asset_batch.v2'}:
        raise ValueError('批量恢复记录损坏')
    keys = {'schema_version', 'entries'}
    if journal['schema_version'] == 'asset_batch.v2':
        from .script_store_support import validate_scheduler_batch

        keys.update({'scheduler_jobs', 'scheduler_runs', 'committed', 'scheduler_history'})
        if set(journal) != keys:
            raise ValueError('调度批量恢复结构损坏')
        validate_scheduler_batch(journal)
    elif set(journal) != keys:
        raise ValueError('批量恢复结构损坏')
    if not isinstance(journal['entries'], list):
        raise ValueError('批量恢复entries必须为列表')
    seen = set()
    for entry in journal['entries']:
        if not isinstance(entry, dict) or set(entry) != {'parts', 'previous', 'stage'}:
            raise ValueError('批量恢复项无效')
        parts = entry['parts']
        _validate_batch_path(parts)
        key = tuple(part.casefold() for part in parts)
        if key in seen or not isinstance(entry['stage'], str) or not re.fullmatch(
                re.escape('.' + parts[-1] + '.stage-') + '[a-f0-9]{32}', entry['stage']):
            raise ValueError('批量恢复路径重复或暂存名无效')
        seen.add(key)
        previous = entry['previous']
        if previous is not None and len(_content_bytes(previous)) > MAX_ASSET_BYTES:
            raise ValueError('批量恢复内容无效')
    return journal


# LLM: 原资产写与Scheduler入口均先持root锁；单job恢复共用store锁，恢复阶段不再次取quota/root锁。
# 函数用途: 在下一个显式可写操作前恢复进程中断的整个批量。
def _recover_batch(root):
    text = _text(root, (_BATCH_FILE,), limit=_BATCH_LIMIT)
    if text is None:
        return
    journal = _decode_journal(text)
    entries = journal['entries']
    paths = [entry['parts'] for entry in entries]
    if journal.get('scheduler_jobs'):
        paths.append(('data', 'scheduler', 'store.json'))
    with _canonical_locks(root, paths):
        if journal.get('committed'):
            _sync_batch_scheduler(root)
            _finish_batch(root, journal)
        else:
            _restore_batch(root, entries, journal.get('scheduler_jobs'), journal.get('scheduler_runs'))


# LLM: scope须显式且不能由描述符推导；纯验证在任何写前，CAS及两阶段发表共享根/原canonical锁，不执行资产。
# 函数用途: 原子保存四类资产草稿，失败回滚全部正式叶，返回已提交描述符与作用域。
def commit_asset_batch(items, *, scope='owner', workspace_root=None, owner_home):
    if scope not in {'owner', 'workspace'} or scope == 'workspace' and workspace_root is None:
        raise ValueError('scope必须为owner或带workspace_root的workspace')
    workspace = Path(workspace_root).absolute() if workspace_root is not None else None
    plans = _prepare_batch(items, scope, workspace)
    result = {'scope': scope, 'workspace_root': str(workspace) if scope == 'workspace' else '',
              'items': [], 'committed': True}
    if not plans:
        return result
    root = workspace_asset_home(workspace, create=True) if scope == 'workspace' else Path(owner_home).absolute()
    if scope == 'owner':
        root.mkdir(parents=True, exist_ok=True)
    with asset_root_lock(root):
        with _canonical_locks(root, [parts for plan in plans for parts in plan['updates']]):
            _finalize_plans(root, plans)
            entries, journal = _batch_entries(root, plans)
            _commit_staged(root, entries, journal)
            result['items'] = [plan['descriptor'].to_dict() for plan in plans]
    return result


# LLM: 附件根只能来自真实发现，中文派生ID不能拼物理路径；nofollow读取文本/二进制，不复制锁或同步元数据。
# 函数用途: 找到准确技能目录并无损冻结其资源，供同一批量事务复制。
def _skill_resources(root, identifier):
    directory = next((path.parent.relative_to(root).parts for path in _strict_skill_paths(root)
        if _skill_record(path, include_body=False, source='workspace')['id'] == identifier), None)
    if directory is None:
        raise FileNotFoundError('项目技能来源目录不存在')
    pending, resources = [()], {}
    while pending:
        relative = pending.pop()
        for name in list_names_beneath(root, (*directory, *relative)):
            if name.startswith('.') or name.endswith('.lock') or not relative and name in {'SKILL.md', 'skill_meta.json'}:
                continue
            parts = (*relative, name)
            _resource_parts('/'.join(parts))
            try:
                text = _file_content(root, (*directory, *parts))
            except OSError:
                _verify_directory_beneath(root, (*directory, *parts))
                pending.append(parts)
            else:
                resources['/'.join(parts)] = text
    return resources


# LLM: Skill正文与附件在同一来源根和技能锁内冻结，先释放来源再获取Owner目标锁；其它资产沿完整单记录读口。
# 函数用途: 冻结一份晋升来源，避免正常并发批量提交产生混版正文和资源。
def _promotion_source(workspace, kind, identifier):
    if kind != 'skill':
        return WorkspaceAssetOverlay(workspace).get_asset(kind, identifier), {}
    safe_asset_id(identifier)
    root = workspace_asset_home(workspace)
    with _read_lock(root, ('.asset-batch.lock',)):
        WorkspaceAssetOverlay(workspace)._check_batch(root)
        path = next((path for path in _strict_skill_paths(root) if skill_asset_id(path.parent.name) == identifier), None)
        if path is None:
            return None, {}
        with _read_lock(path.parent, ('skill_meta.json.lock',)):
            record = _project(_skill_record(path, include_body=True, source='workspace'), 'workspace', workspace)
            resources = _skill_resources(root, identifier)
    return record, resources


# LLM: 晋升来源是锁内冻结的单版本，目标CAS由commit_asset_batch在Owner锁内检查；来源投影不授跨Owner权限。
# 函数用途: 将项目资产复制到Owner原canonical存储，保留原项目版本与晋升出处。
def promote_to_owner(asset_kind, asset_id, *, workspace_root, owner_home, expected_revision=None):
    workspace = Path(workspace_root).absolute()
    record, resources = _promotion_source(workspace, asset_kind, asset_id)
    if record is None:
        raise FileNotFoundError('项目资产不存在')
    descriptor = AssetDescriptor.from_dict({key: value for key, value in record.items() if key in _DESCRIPTOR_FIELDS})
    descriptor = replace(descriptor, metadata={**descriptor.metadata,
        'promoted_from_workspace': str(workspace), 'promoted_from_revision': descriptor.revision})
    item = {'descriptor': descriptor.to_dict(), 'expected_revision': expected_revision}
    if asset_kind == 'script':
        item.update({key: record[key] for key in ('code', 'entrypoint', 'interpreter', 'timeout_seconds')})
    elif asset_kind == 'workflow':
        item.update(steps=record['steps'], body_markdown=record.get('body_markdown', ''))
    elif asset_kind == 'skill':
        item.update(body_markdown=record['body_markdown'], files=resources)
    else:
        item['schedule_spec'] = record['schedule_spec']
    return commit_asset_batch([item], owner_home=owner_home)
