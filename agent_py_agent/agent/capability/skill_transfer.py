# LLM: 工具只准备本owner清单并只读本owner回执；授权/接收仍由认证人类入口调用，原双端grant/hash/expiry不变。
# 模块用途: 沿原registry保存意图与确认，提供字段错误和双向回执；引用与索引均不授予跨owner正文权限。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import re
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path

from ..common.nofollow_fs import (
    list_names_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_bytes_atomic_beneath,
)
from ..user_space.interaction_refs import interaction_key
from ..user_space.owner_quota import OwnerQuotaChange
from ..user_space.owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home
from .self_install_switches import read_fresh_capability_switch
from .skill_learning_store import MAX_PENDING_REQUESTS, SkillLearningStore, SkillLearningStoreError
from .skill_transfer_import import _submit_import_locked
from .skill_transfer_manifest import (
    MAX_TRANSFER_BYTES,
    MAX_TRANSFER_FILES,
    MAX_TRANSFER_SKILLS,
    SkillTransferError,
    build_skill_bundles,
    host_owner,
    manifest_hash,
    public_manifest,
)


# LLM: 继承原错误协议以保留所有调用方catch；message仅为宿主字段说明，不携参数值、正文或底层异常。
# 类用途: 在不修改共享manifest模块的情况下携带安全的字段级修正提示。
class SkillTransferInputError(SkillTransferError):
    # LLM: code保持原结构化拒绝码，message只供展示，不参与授权。
    # 函数用途: 构造带准确参数说明的迁移错误。
    def __init__(self, message, code='SKILL_TRANSFER_ARGUMENTS_INVALID'):
        super().__init__(code)
        self.message = message
        self.args = (message,)


# LLM: 仅检验选择项形状；来源、hash对应关系、路径和秘密仍交给原manifest硬门，不复制安全裁决。
# 函数用途: 在读取或写入前指出skills条目缺失字段。
def _validate_selections(selections):
    if not isinstance(selections, list) or len(selections) > MAX_TRANSFER_SKILLS:
        raise SkillTransferInputError(f'skills 必须为数组，最多 {MAX_TRANSFER_SKILLS} 项，每项包含 stable_id 与 content_sha256。')
    for index, item in enumerate(selections):
        prefix = f'skills[{index}]'
        if not isinstance(item, dict) or set(item) - {'stable_id', 'content_sha256', 'resources', 'target_name'}:
            raise SkillTransferInputError(f'{prefix} 必须为对象，仅含 stable_id、content_sha256、resources、target_name。', 'SKILL_TRANSFER_MANIFEST_INVALID')
        if not isinstance(item.get('stable_id'), str) or not item['stable_id'].strip():
            raise SkillTransferInputError(f'{prefix} 缺少字段 stable_id（非空字符串）。')
        if not isinstance(item.get('content_sha256'), str) or re.fullmatch(r'[0-9a-f]{64}', item['content_sha256']) is None:
            raise SkillTransferInputError(f'{prefix}.content_sha256 必须为64位小写十六进制摘要。')
        resources = item.get('resources', [])
        if not isinstance(resources, list) or len(resources) >= MAX_TRANSFER_FILES or not all(isinstance(value, str) for value in resources):
            raise SkillTransferInputError(f'{prefix}.resources 必须为相对路径字符串数组，少于 {MAX_TRANSFER_FILES} 项。', 'SKILL_TRANSFER_MANIFEST_INVALID')


# LLM: 纯形状检查可在工具claim前执行；memory-only复刻沿原语义允许，不误收紧既有双端授权功能。
# 函数用途: 检查prepare的技能、复刻布尔值和显式记忆候选组合。
def validate_transfer_selection(selections, *, clone=False, memory_candidates=()):
    if type(clone) is not bool:
        raise SkillTransferInputError('clone 必须为布尔值 true / false。', 'SKILL_TRANSFER_MANIFEST_INVALID')
    _validate_selections(selections)
    if not selections and not memory_candidates:
        raise SkillTransferInputError('action=prepare 缺少必填参数 skills（非空数组，每项需含 stable_id 与 content_sha256；复刻可显式选择 memory_candidates）。')
    if memory_candidates and not clone:
        raise SkillTransferInputError('memory_candidates 仅可在 clone=true 时提供。', 'SKILL_TRANSFER_MEMORY_MANIFEST_INVALID')


# LLM: 开关只启用入口，不替代双端用户授权；所有准备/读取/确认均现读，关闭后旧ref不能继续接收。
# 函数用途: 在迁移入口统一核对当前能力设置。
def require_transfer_enabled(host):
    if not read_fresh_capability_switch(host, 'skill_transfers_enabled'):
        raise SkillTransferError('SKILL_TRANSFER_DISABLED')


# LLM: owner只接受原规范三元组；格式问题显示字段名，不回显身份值或路径，不创建目标owner。
# 函数用途: 校验准确target身份并提供缺字段的修正格式。
def transfer_owner(root, value):
    if not isinstance(value, dict) or set(value) != {'provider', 'owner_kind', 'owner_id'}:
        raise SkillTransferInputError('target（target_owner）必须为对象，准确包含 provider、owner_kind、owner_id。')
    if not all(isinstance(value[key], str) and value[key].strip() for key in value):
        raise SkillTransferInputError('target.provider、target.owner_kind、target.owner_id 必须为非空字符串。')
    try:
        identity = OwnerIdentity(**value)
        if identity.owner_kind not in {'user', 'group'} and identity != OwnerIdentity.local_main():
            raise ValueError('invalid kind')
        resolve_owner_home(root, identity)
    except (TypeError, ValueError):
        raise SkillTransferInputError('target 的 provider、owner_kind、owner_id 必须为规范owner三元组。', 'SKILL_TRANSFER_OWNER_INVALID') from None
    return asdict(identity)


# LLM: 先指出字段问题，再走原快照/manifest硬门；bundle及元数据只在原quota→state事务保存，不给目标读权限。
# 函数用途: 固定分享/复刻清单并保存可查询摘要，尚未授权或导入目标内容。
def prepare_transfer(agent, target, selections, *, clone=False, memory_candidates=()):
    require_transfer_enabled(agent)
    target = transfer_owner(agent.home_paths.root, target)
    validate_transfer_selection(selections, clone=clone, memory_candidates=memory_candidates)
    if target == host_owner(agent):
        raise SkillTransferInputError('target 必须与当前 source_owner 不同。', 'SKILL_TRANSFER_MANIFEST_INVALID')
    from .skill_transfer_memory import build_memory_candidates
    memories = build_memory_candidates(agent, memory_candidates)
    manifest = {'schema_version': 'skill_transfer.v1', 'source_owner': host_owner(agent), 'receiver_owner': target,
        'clone': clone, 'skills': build_skill_bundles(agent, selections), 'memory_candidates': memories}
    if not manifest['skills'] and not memories:
        raise SkillTransferInputError('skills 或 memory_candidates 至少提供一项有效选择。', 'SKILL_TRANSFER_MANIFEST_INVALID')
    operation_id = uuid.uuid4().hex
    payload = {'operation_id': operation_id, 'source_owner': manifest['source_owner'], 'receiver_owner': target,
        'manifest_hash': manifest_hash(manifest), 'expires_at': time.time() + 86400}
    reference = _sign_reference(agent.home_paths.owner_home_dir, payload)
    store = SkillLearningStore.for_home(agent.home_paths)
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    with store.state_lock() as admission:
        registry = _expire_exports(store, store.load_registry())
        if sum(row.get('role') == 'export' and row.get('status') in {'draft', 'authorized'} for row in registry.import_operations.values()) >= MAX_PENDING_REQUESTS:
            raise SkillTransferError('SKILL_TRANSFER_PENDING_LIMIT')
        path = store.directory / 'transfers' / operation_id / 'bundle.json'
        row = {**payload, 'role': 'export', 'status': 'draft', 'target_names': [], 'reference': reference,
            'skill_names': [item['source_name'] for item in manifest['skills']]}
        updated = replace(registry, import_operations={**registry.import_operations, 'export:' + operation_id: row})
        metadata = json.dumps(updated.to_record(), ensure_ascii=False, indent=2, sort_keys=True) + '\n'
        admission.check([OwnerQuotaChange(path, len(raw)), OwnerQuotaChange(store.registry_path, len(metadata.encode()))])
        _save_prepared_export(store, operation_id, raw, updated, reference)
    return {'ok': True, 'reference': reference, 'manifest': public_manifest(manifest), 'manifest_hash': payload['manifest_hash'],
        'status': 'draft', 'message': '尚未导出。请本人核对清单和目标后，在来源助手使用 /skills transfer authorize <reference>。'}


# LLM: 引用仅携owner/hash/expiry并按原交互key独立用途MAC；不从可篡改JSON选私有路径，验签前不创建owner或key。
# 函数用途: 读取受保护导出及精确清单，只有当前source或已授权receiver可见其元数据。
def load_transfer(host, reference):
    payload = _verify_reference(host.home_paths.root, reference)
    identity = OwnerIdentity(**payload['source_owner'])
    source_home = home_paths_with_owner(host.home_paths, resolve_owner_home(host.home_paths.root, identity))
    store = SkillLearningStore.for_home(source_home)
    with store.state_lock():
        return _load_transfer_locked(host, reference, payload, store)


# LLM: 调用方必须持source原state锁；grant/manifest读取和撤回同锁，不新建权限账，payload由原MAC验证。
# 函数用途: 在稳定来源授权临界区内读取准确导出快照。
def _load_transfer_locked(host, reference, payload, store):
    row = store.load_registry().import_operations.get('export:' + payload['operation_id'])
    if not row or row.get('reference') != reference or row.get('manifest_hash') != payload['manifest_hash']:
        raise SkillTransferError('SKILL_TRANSFER_REFERENCE_INVALID')
    current = host_owner(host)
    if current != payload['source_owner'] and (current != payload['receiver_owner'] or row.get('status') != 'authorized'):
        raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
    raw = read_bytes_beneath(store.directory, ('transfers', payload['operation_id'], 'bundle.json'),
        max_bytes=MAX_TRANSFER_BYTES * 2, require_dir_fd=True)
    manifest = json.loads(raw) if raw is not None else None
    if not isinstance(manifest, dict) or manifest_hash(manifest) != payload['manifest_hash']:
        raise SkillTransferError('SKILL_TRANSFER_SOURCE_CHANGED')

    return payload, row, manifest, store


# LLM: 只有原认证人类控制入口可调用；scope owner须正是source，grant固定当前actor与完整hash，模型工具没有此动作。
# 函数用途: 本人在来源侧明确授权精确清单，或撤回尚未接收的授权，不自动发消息给其他人。
def authorize_transfer(host, reference, actor, *, revoke=False):
    require_transfer_enabled(host)
    payload, row, manifest, store = load_transfer(host, reference)
    if host_owner(host) != payload['source_owner']:
        raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
    with store.state_lock():
        registry = store.load_registry()
        current = registry.import_operations.get('export:' + payload['operation_id'])
        if current != row:
            raise SkillTransferError('SKILL_TRANSFER_OPERATION_CONFLICT')
        grant = {'kind': 'source', 'owner': host_owner(host), 'actor': actor, 'manifest_hash': payload['manifest_hash'],
                 'reference': reference, 'confirmed_at': time.time()}
        updated = {**row, 'status': 'revoked' if revoke else 'authorized', 'source_grant': grant}
        store.save_registry(replace(registry, import_operations={**registry.import_operations, 'export:' + payload['operation_id']: updated}))
    return {'ok': True, 'status': updated['status'], 'manifest': public_manifest(manifest), 'reference': reference}


# LLM: 原双端quota/state锁保持至目标commit；成功后仅镜像元数据回执，不反转成功导入或把回执当授权。
# 函数用途: 人类确认后按原幂等账导入，向来源原账补充可查询的落地结果。
def accept_transfer(host, reference, actor):
    require_transfer_enabled(host)
    payload = _verify_reference(host.home_paths.root, reference)
    if host_owner(host) != payload['receiver_owner']:
        raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
    source_home = home_paths_with_owner(host.home_paths, resolve_owner_home(host.home_paths.root,
        OwnerIdentity(**payload['source_owner'])))
    source = SkillLearningStore.for_home(source_home)
    target = SkillLearningStore.for_home(host.home_paths)
    # 两端均按canonical owner home排序，每个owner仍先quota后state；反向投递也不能形成AB/BA锁环。
    stores = sorted((source, target), key=lambda store: str(store.skills_root.parent))
    with ExitStack() as stack:
        admissions = {str(store.skills_root.parent): stack.enter_context(store.state_lock()) for store in stores}
        require_transfer_enabled(host)
        payload = _verify_reference(host.home_paths.root, reference)
        _payload, row, manifest, _store = _load_transfer_locked(host, reference, payload, source)
        if row.get('status') != 'authorized':
            raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
        grant = {'kind': 'receiver', 'owner': host_owner(host), 'actor': actor,
            'manifest_hash': payload['manifest_hash'], 'reference': reference, 'confirmed_at': time.time()}
        names = {entry.name for entry in host.skills_service.snapshot_for().entries}
        result = _submit_import_locked(host, target, admissions[str(target.skills_root.parent)], manifest,
            payload['operation_id'], source_grant=row['source_grant'], receiver_grant=grant, taken_names=names)
        _save_transfer_receipt(source, payload, result)
    host.skills_service.clear_cache()
    return result


# LLM: 调用方已持来源原quota/state锁；仅保存目标原提交结果，不改变authorized状态或双端grant，异常只警告。
# 函数用途: 将成功导入回执镜像到来源原导出行，失败可在原编号重放时补回。
def _save_transfer_receipt(store, payload, result):
    try:
        registry = store.load_registry()
        key = 'export:' + payload['operation_id']
        row = registry.import_operations[key]
        if row.get('receipt') == result:
            return
        store.save_registry(replace(registry, import_operations={**registry.import_operations, key: {**row, 'receipt': result}}))
    except (SkillLearningStoreError, SkillTransferError, OSError, RuntimeError, ValueError, KeyError):
        logging.getLogger(__name__).warning('Skill投递已提交，来源回执暂未完整保存；可按原引用查目标或重放原确认。')


# LLM: 只读取当前store固定目录；旧导出未保存名称摘要时，从同owner原bundle投影名称，不返回资源字节。
# 函数用途: 获取一条本地原导出的技能名称列表。
def _export_skill_names(store, key, row):
    if 'skill_names' in row:
        return row['skill_names']
    identifier = key.removeprefix('export:')
    if uuid.UUID(identifier).hex != identifier:
        raise SkillTransferInputError('import_operations.operation_id 必须为原UUID编号。', 'SKILL_TRANSFER_MANIFEST_INVALID')
    raw = read_bytes_beneath(store.directory, ('transfers', identifier, 'bundle.json'), max_bytes=MAX_TRANSFER_BYTES * 2, require_dir_fd=True)
    if raw is None:
        return []
    manifest = json.loads(raw)
    if manifest_hash(manifest) != row.get('manifest_hash'):
        raise SkillTransferError('SKILL_TRANSFER_SOURCE_CHANGED')
    return [item.get('source_name', item['name']) for item in manifest['skills']]


# LLM: 仅显示已有状态与原提交回执；缺回执必须是未知，绝不把authorized视为对方已收到或导入。
# 函数用途: 生成不含bundle、actor、私有路径的投递列表行和人工命令提示。
def _transfer_status_row(reference, row, skills, *, outgoing):
    receipt = row.get('receipt', {}) if outgoing else row.get('result', {})
    if not receipt and isinstance(row.get('imported'), list):
        receipt = {'ok': True, 'imported': row['imported']}
    imported = receipt.get('imported') if receipt.get('ok') is True else None
    status = row.get('status', 'unknown')
    commands = [f'/skills transfer show {reference}']
    if outgoing and status == 'draft':
        commands.append(f'/skills transfer authorize {reference}')
    elif not outgoing and status in {'preparing', 'failed'}:
        commands.append(f'/skills transfer accept {reference}')
    return {'reference': reference, 'status': status, 'source_owner': row['source_owner'],
        'target_owner': row.get('receiver_owner', row.get('target_owner')), 'skills': skills, 'imported': imported,
        'delivery_status': 'committed' if imported is not None else 'unconfirmed', 'next_commands': commands}


# LLM: tx索引只在原owner目录无跟链读取，owner字段仅筛选本地展示，不参与授权；坏记录明确失败，不能假装空。
# 函数用途: 读取已有tx索引的本地元数据，不访问其它owner目录。
def _local_transfer_indexes(store, owner):
    rows = {}
    for name in list_names_beneath(store.directory, ('transfers',)):
        if not name.startswith('tx_') or not name.endswith('.json'):
            continue
        raw = read_bytes_beneath(store.directory, ('transfers', name), max_bytes=MAX_TRANSFER_BYTES, require_dir_fd=True)
        row = json.loads(raw) if raw is not None else None
        if not isinstance(row, dict):
            raise SkillTransferInputError('transfers.tx_*.json 必须为有效投递对象。', 'SKILL_TRANSFER_MANIFEST_INVALID')
        outgoing = row.get('source_owner') == owner
        if not outgoing and row.get('receiver_owner', row.get('target_owner')) != owner:
            continue
        reference = row.get('reference')
        if not isinstance(reference, str) or not reference:
            raise SkillTransferInputError('transfers.tx_*.json 缺少非空 reference。', 'SKILL_TRANSFER_MANIFEST_INVALID')
        rows[reference] = (outgoing, _transfer_status_row(reference, row, row.get('skills', []), outgoing=outgoing))
    return rows


# LLM: 只扫描当前owner的索引和原registry；canonical行优先去重，不读接收方私有目录，不创建目录或更新状态。
# 函数用途: 列出本身份发出和已接收的投递回执，区分未确认与真实落地。
def list_transfer_statuses(agent):
    require_transfer_enabled(agent)
    store = SkillLearningStore.for_home(agent.home_paths)
    owner = host_owner(agent)
    rows = _local_transfer_indexes(store, owner)
    for key, row in store.load_registry().import_operations.items():
        outgoing = row.get('role') == 'export' and row.get('source_owner') == owner
        incoming = row.get('role') == 'import' and row.get('receiver_owner') == owner
        if not outgoing and not incoming:
            continue
        reference = row.get('reference') if outgoing else row.get('source_grant', {}).get('reference')
        if not reference:
            raise SkillTransferInputError('import_operations.reference 缺失，无法核验投递回执。', 'SKILL_TRANSFER_MANIFEST_INVALID')
        skills = _export_skill_names(store, key, row) if outgoing else row.get('target_names', [])
        rows[reference] = (outgoing, _transfer_status_row(reference, row, skills, outgoing=outgoing))
    return {'ok': True, 'outgoing': [row for outgoing, row in rows.values() if outgoing],
        'incoming': [row for outgoing, row in rows.values() if not outgoing]}


# LLM: MAC带独立skill-transfer用途，不复用私人卡actor放行规则；payload只含稳定身份和清单摘要。
# 函数用途: 为一条导出生成不可篡改跨owner引用。
def _sign_reference(owner_home, payload):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    signature = hmac.new(interaction_key(Path(owner_home), create=True), b'skill-transfer.v1\0' + raw, hashlib.sha256).hexdigest()
    return 'st1.' + encoded + '.' + signature


# LLM: 受限长度/版本/owner/MAC/期限全部满足才访问bundle；失败无路径探测回显，不重置已有坏key。
# 函数用途: 验证准确清单引用，转发给错误owner仍不取得读取或接收权限。
def _verify_reference(root, reference):
    try:
        if not isinstance(reference, str) or len(reference) > 4096:
            raise ValueError('invalid')
        prefix, encoded, signature = reference.split('.')
        if prefix != 'st1' or len(signature) != 64:
            raise ValueError('invalid')
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        payload = json.loads(raw)
        if (not isinstance(payload, dict) or set(payload) != {'operation_id', 'source_owner', 'receiver_owner', 'manifest_hash', 'expires_at'}
                or type(payload['expires_at']) not in (int, float) or not math.isfinite(payload['expires_at']) or payload['expires_at'] <= time.time()
                or uuid.UUID(payload['operation_id']).hex != payload['operation_id']):
            raise ValueError('invalid')
        for key in ('source_owner', 'receiver_owner'):
            transfer_owner(root, payload[key])
        if payload['source_owner'] == payload['receiver_owner']:
            raise ValueError('invalid')
        home = resolve_owner_home(root, OwnerIdentity(**payload['source_owner'])).home_dir
        expected = hmac.new(interaction_key(home), b'skill-transfer.v1\0' + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('invalid')
        return payload
    except (OSError, RuntimeError, KeyError, TypeError, ValueError):
        raise SkillTransferError('SKILL_TRANSFER_REFERENCE_INVALID') from None


# LLM: 沿原registry和quota/state锁清理自己发出的已过期/撤回bundle，仅删摘要仍匹配的精确普通文件；不扫描其它owner目录。
# 函数用途: 限制长期暂存，保留无正文操作元数据；坏/被改快照拒绝继续堆新导出，避免误删用户内容。
def _expire_exports(store, registry):
    operations = dict(registry.import_operations)
    changed = False
    for key, row in operations.items():
        if row.get('role') != 'export' or row.get('status') == 'expired':
            continue
        if row.get('status') != 'revoked' and row.get('expires_at', float('inf')) > time.time():
            continue
        identifier = row.get('operation_id')
        if not isinstance(identifier, str) or uuid.UUID(identifier).hex != identifier:
            raise SkillTransferError('SKILL_TRANSFER_OPERATION_CONFLICT')
        parts = ('transfers', identifier, 'bundle.json')
        raw = read_bytes_beneath(store.directory, parts, max_bytes=MAX_TRANSFER_BYTES * 2, require_dir_fd=True)
        if raw is not None:
            if manifest_hash(json.loads(raw)) != row.get('manifest_hash'):
                raise SkillTransferError('SKILL_TRANSFER_OPERATION_CONFLICT')
            unlink_file_beneath(store.directory, parts)
        operations[key] = {**row, 'status': 'expired'}
        changed = True
    if changed:
        registry = replace(registry, import_operations=operations)
        store.save_registry(registry)
    return registry


# LLM: bundle和原registry共同预检后写入；明确未登记时只撤回本次新快照，已登记的真实结果不因回执异常被删。
# 函数用途: 保存本owner准备态，失败不遗留无权目标可见的导出，也不把可能成功的记录删成假失败。
def _save_prepared_export(store, operation_id, raw, updated, reference):
    parts = ('transfers', operation_id, 'bundle.json')
    try:
        write_bytes_atomic_beneath(store.directory, parts, raw)
        store.save_registry(updated)
    except (OSError, RuntimeError, TypeError, ValueError):
        current = store.load_registry().import_operations.get('export:' + operation_id)
        if current and current.get('reference') == reference:
            return
        existing = read_bytes_beneath(store.directory, parts, max_bytes=MAX_TRANSFER_BYTES * 2, require_dir_fd=True)
        if existing == raw:
            unlink_file_beneath(store.directory, parts)
        raise
