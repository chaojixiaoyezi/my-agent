# LLM: 工具只准备本owner清单；授权与接收只由认证人类控制入口调用，双端grant绑定完整hash、稳定owner和有效期。
# 模块用途: 沿原学习registry保存导出意图及双端确认，签名引用不是工具权限、不能让群直接读取别人的私人目录。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath, unlink_file_beneath, write_bytes_atomic_beneath
from ..user_space.interaction_refs import interaction_key
from ..user_space.owner_quota import OwnerQuotaChange
from ..user_space.owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home
from .self_install_switches import read_fresh_capability_switch
from .skill_learning_store import MAX_PENDING_REQUESTS, SkillLearningStore
from .skill_transfer_import import _submit_import_locked
from .skill_transfer_manifest import (
    MAX_TRANSFER_BYTES,
    SkillTransferError,
    build_skill_bundles,
    host_owner,
    manifest_hash,
    public_manifest,
)


# LLM: 开关只启用入口，不替代双端用户授权；所有准备/读取/确认均现读，关闭后旧ref不能继续接收。
# 函数用途: 在迁移入口统一核对当前能力设置。
def require_transfer_enabled(host):
    if not read_fresh_capability_switch(host, 'skill_transfers_enabled'):
        raise SkillTransferError('SKILL_TRANSFER_DISABLED')


# LLM: owner只接受规范三元组，不能通过自造provider/kind访问任意路径或用模型身份覆盖宿主owner。
# 函数用途: 校验工具给出的准确目标身份，纯解析不创建目标owner。
def transfer_owner(root, value):
    if not isinstance(value, dict) or set(value) != {'provider', 'owner_kind', 'owner_id'}:
        raise SkillTransferError('SKILL_TRANSFER_OWNER_INVALID')
    identity = OwnerIdentity(**value)
    if identity.owner_kind not in {'user', 'group'} and identity != OwnerIdentity.local_main():
        raise SkillTransferError('SKILL_TRANSFER_OWNER_INVALID')
    resolve_owner_home(root, identity)
    return asdict(identity)


# LLM: 准备阶段只读取当前owner明确选择的快照，不给目标读权限；bundle/导出记录均在原quota→state事务保存。
# 函数用途: 固定待分享/复刻清单并返回双端可核对引用，尚未授权或导入任何目标内容。
def prepare_transfer(agent, target, selections, *, clone=False, memory_candidates=()):
    require_transfer_enabled(agent)
    target = transfer_owner(agent.home_paths.root, target)
    if type(clone) is not bool or target == host_owner(agent):
        raise SkillTransferError('SKILL_TRANSFER_MANIFEST_INVALID')
    if memory_candidates and not clone:
        raise SkillTransferError('SKILL_TRANSFER_MEMORY_MANIFEST_INVALID')
    from .skill_transfer_memory import build_memory_candidates
    memories = build_memory_candidates(agent, memory_candidates)
    manifest = {'schema_version': 'skill_transfer.v1', 'source_owner': host_owner(agent), 'receiver_owner': target,
        'clone': clone, 'skills': build_skill_bundles(agent, selections), 'memory_candidates': memories}
    if not manifest['skills'] and not memories:
        raise SkillTransferError('SKILL_TRANSFER_MANIFEST_INVALID')
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
        row = {**payload, 'role': 'export', 'status': 'draft', 'target_names': [], 'reference': reference}
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


# LLM: 接收按两端canonical owner顺序持原quota/state锁，复核source grant并保持到目标commit；撤回与接收线性化，权限不复制。
# 函数用途: 当前目标人类确认后提交原Skill登记，重复/并发复刻沿接收owner原幂等账裁决。
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
    host.skills_service.clear_cache()
    return result


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
