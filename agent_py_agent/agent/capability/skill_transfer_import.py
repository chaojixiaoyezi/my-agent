# LLM: 接收只持原owner quota→Skill state事务，import_operations是唯一提交/一次复刻事实；preparing路径由原snapshot隐藏。
# 模块用途: 完整预检、批量导入、失败回滚与崩溃同编号重试，不绕过原Skill登记/版本/资源容量。
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from types import SimpleNamespace

from ..common.nofollow_fs import list_names_beneath, read_bytes_beneath, write_bytes_atomic_beneath
from ..common.nofollow_tree import remove_tree_beneath
from ..contracts.gates.skill_guard import install_decision, scan_skill
from ..user_space.owner_quota import OwnerQuotaChange
from .skill_learning_store import LearnedSkill, SkillLearningStoreError, utc_now
from .skill_snapshot import skill_content_sha256
from .skill_transfer_manifest import (
    SkillTransferError,
    file_bytes,
    host_owner,
    manifest_hash,
    resource_parts,
)
from .skill_transfer_memory import memory_import_transaction
from .skills import parse_skill_file


# LLM: 只供已持当前目标原quota→state事务的宿主调用；source锁由双端入口按统一owner顺序保持到commit，不允许模型选择事务。
# 函数用途: 复用整批导入原实现，避免双端授权临界区内重入不可重入的文件锁。
def _submit_import_locked(host, store, admission, manifest, operation_id, *, source_grant, receiver_grant, taken_names=()):
    digest = manifest_hash(manifest)
    if (host_owner(host) != manifest.get('receiver_owner') or source_grant.get('kind') != 'source'
            or receiver_grant.get('kind') != 'receiver' or source_grant.get('owner') != manifest.get('source_owner')
            or receiver_grant.get('owner') != manifest.get('receiver_owner')
            or any(grant.get('manifest_hash') != digest or not isinstance(grant.get('actor'), dict)
                   for grant in (source_grant, receiver_grant))):
        raise SkillTransferError('SKILL_TRANSFER_NOT_AUTHORIZED')
    registry = store.load_registry()
    previous = registry.import_operations.get(operation_id)
    if previous and previous.get('manifest_hash') != digest:
        raise SkillTransferError('SKILL_TRANSFER_OPERATION_CONFLICT')
    if previous and previous.get('status') == 'committed':
        return previous['result']
    if manifest['clone'] and any(row.get('role') == 'import' and row.get('clone') is True and row.get('status') == 'committed'
            for row in registry.import_operations.values()):
        raise SkillTransferError('SKILL_TRANSFER_CLONE_ALREADY_USED')
    if previous and previous.get('status') == 'preparing':
        _rollback_files(store, previous)
    prepared, duplicates = _prepare_entries(store, registry, manifest, operation_id, taken_names)
    operation = {'role': 'import', 'status': 'preparing', 'target_names': [item['name'] for item in prepared],
        'clone': manifest['clone'], 'manifest_hash': digest, 'source_owner': manifest['source_owner'],
        'receiver_owner': manifest['receiver_owner'], 'source_grant': source_grant, 'receiver_grant': receiver_grant,
        'files': {item['name']: {path: hashlib.sha256(raw).hexdigest() for path, raw in item['files'].items()} for item in prepared}}
    operations = {**registry.import_operations, operation_id: operation}
    pending = replace(registry, import_operations=operations)
    additions = {item['name']: _learned_record(item, operation_id) for item in prepared}
    result = {'ok': True, 'operation_id': operation_id, 'imported': list(additions), 'duplicates': duplicates,
              'clone_completed': manifest['clone'], 'memory_candidates': len(manifest.get('memory_candidates', []))}
    final = replace(pending, skills={**registry.skills, **additions}, import_operations={**operations,
        operation_id: {**operation, 'status': 'committed', 'result': result}})
    with memory_import_transaction(host, manifest, operation_id, admission) as memory:
        plan = SimpleNamespace(registry=registry, entries=prepared, operation_id=operation_id,
            pending=pending, final=final, operation=operation)
        return _commit_locked(host, store, plan, memory, admission)

# LLM: 原quota/state与候选锁均已持有；成功唯一写在原registry，异常先判断是否已提交再回滚，不能因回执错破坏成功版本。
# 函数用途: 预检整批并推进preparing到committed，恢复同编号崩溃时不重复计成功。
def _commit_locked(host, store, plan, memory, admission):
    registry, prepared, operation_id = plan.registry, plan.entries, plan.operation_id
    pending, final, operation = plan.pending, plan.final, plan.operation
    operation['memory_candidate_ids'] = memory.ids
    final.import_operations[operation_id]['memory_candidate_ids'] = memory.ids
    _check_batch(admission, store, prepared, pending, final, memory.changes)
    store.save_registry(pending)
    try:
        _write_entries(host, store, prepared, admission)
        memory.apply()
        store.save_registry(final)
    except (OSError, RuntimeError, TypeError, ValueError, SkillLearningStoreError):
        current = store.load_registry()
        if current.import_operations.get(operation_id, {}).get('status') == 'committed':
            memory.finish()
            return current.import_operations[operation_id]['result']
        _rollback_files(store, operation)
        failed = {**operation, 'status': 'failed', 'target_names': [], 'files': {}}
        store.save_registry(replace(registry, import_operations={**registry.import_operations, operation_id: failed}))
        raise
    memory.finish()
    return final.import_operations[operation_id]['result']

# LLM: 只新增或精确去重，不静默覆盖目标同名/用户修改/删除阻止名；同来源不同revision需新确认名字。
# 函数用途: 在原锁内计算目标完整字节和原登记新增项，不提前发布半份内容。
def _prepare_entries(store, registry, manifest, operation_id, taken_names):
    result, duplicates = [], []
    for item in manifest['skills']:
        name = item['name']
        current = registry.skills.get(name)
        provenance = {'origin': item['origin'], 'files_hash': manifest_hash(item['files'])}
        if current is not None:
            known = current.maintenance.get('transfer_source')
            active = store.skill_path(name)
            raw = read_bytes_beneath(store.skills_root, active.relative_to(store.skills_root).parts, require_dir_fd=True)
            if known == provenance and raw is not None and skill_content_sha256(raw.decode()) == current.sha256 and _resources_current(store.skills_root, active.parent.relative_to(store.skills_root).parts, item):
                duplicates.append(name)
                continue
            raise SkillTransferError('SKILL_TRANSFER_NAME_CONFLICT')
        if name in registry.blocked_names or name in taken_names or (store.learned_root / name).exists() or (store.learned_root / name).is_symlink():
            raise SkillTransferError('SKILL_TRANSFER_NAME_CONFLICT')
        files = {path: file_bytes(record) for path, record in item['files'].items()}
        body = files['SKILL.md'].decode()
        body, count = re.subn(r'(?m)^name:[^\n]*$', 'name: ' + name, body, count=1)
        if count != 1:
            raise SkillTransferError('SKILL_TRANSFER_SNAPSHOT_INVALID')
        files['SKILL.md'] = body.encode()
        result.append({'name': name, 'files': files, 'sha256': skill_content_sha256(body), 'provenance': provenance})
    return result, duplicates


# LLM: 最终Skill/资源/版本和两阶段registry都计精确字节；同一quota锁隔离并发学习/导入，不用旧余量分别放行。
# 函数用途: 在任何准备记录/发布前预检整个提交量，容量不足不生成半份Skill或成功计数。
def _check_batch(admission, store, entries, pending, final, memory_changes=()):
    changes = list(memory_changes)
    for item in entries:
        for path, raw in item['files'].items():
            resource_parts(path)
            changes.append(OwnerQuotaChange(store.learned_root / item['name'] / path, len(raw)))
            changes.append(OwnerQuotaChange(store.directory / 'transfer_staging' / item['name'] / path, len(raw)))
        changes.append(OwnerQuotaChange(store.versions_dir / item['name'] / 'v1.md', len(item['files']['SKILL.md'])))
    size = max(len((json.dumps(row.to_record(), ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()) for row in (pending, final))
    changes.append(OwnerQuotaChange(store.registry_path, size))
    admission.check(changes)


# LLM: 暂存读写/清理锚定原store目录且也计入quota；guard拒绝则无目标发布，脚本只复制不执行，权限仍按接收owner原工具门。
# 函数用途: 校验完整包后写目标文件与版本，失败由同一事务回滚。
def _write_entries(host, store, entries, admission):
    for item in entries:
        stage = store.directory / 'transfer_staging' / item['name']
        admission.check([OwnerQuotaChange(stage / path, len(raw)) for path, raw in item['files'].items()])
        try:
            for path, raw in item['files'].items():
                write_bytes_atomic_beneath(store.directory, ('transfer_staging', item['name'], *resource_parts(path)), raw)
            card = parse_skill_file(stage / 'SKILL.md', source='owner', require_frontmatter=True)
            guard = scan_skill(stage, source='external', config=host.config)
            if card.name != item['name'] or not install_decision(guard)[0]:
                raise SkillTransferError('SKILL_TRANSFER_GUARD_BLOCKED')
            for path, raw in item['files'].items():
                write_bytes_atomic_beneath(store.skills_root, ('learned', item['name'], *resource_parts(path)), raw)
            store.save_version(item['name'], 1, item['files']['SKILL.md'].decode())
        finally:
            remove_tree_beneath(store.directory, ('transfer_staging', item['name']))


# LLM: 新条目保存原来源身份/hash但不保存source运行权限/工具授权；本地版本独立，从1开始，不伪造学习质量。
# 函数用途: 为原registry构造一条准确导入版本事实。
def _learned_record(item, operation_id):
    now = utc_now()
    return LearnedSkill(item['name'], 1, item['sha256'], now, now, revision=1,
        last_operation_id=operation_id, maintenance={'transfer_source': item['provenance'], 'imported': True})


# LLM: 只清本操作新建且尚未提交的精确目录；旧用户/其他操作不属于此清理范围，不以目录名猜归属。
# 函数用途: 撤销失败或崩溃准备中的新文件，保留原有效Skill和一次复刻资格。
def _rollback_files(store, operation):
    for name in operation.get('target_names', []):
        if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name) is None:
            raise SkillTransferError('SKILL_TRANSFER_OPERATION_CONFLICT')
        if name in store.load_registry().skills:
            raise SkillTransferError('SKILL_TRANSFER_OPERATION_CONFLICT')
        expected = operation.get('files', {}).get(name, {})
        for root, parts, hashes in ((store.skills_root, ('learned', name), expected),
                (store.directory, ('versions', name), {'v1.md': expected.get('SKILL.md')})):
            _assert_owned_tree(root, parts, hashes)
            remove_tree_beneath(root, parts)


# LLM: 回滚从原owner受信根逐段no-follow读取与删除；任何额外文件/链接/修改都阻断清理，嵌套父路径不得晋升可信根。
# 函数用途: 在移除失败暂存前逐文件复核摘要与目录集合。
def _assert_owned_tree(root, parts, expected):
    parents = {tuple(path.split('/')[:index]) for path in expected
        for index in range(1, len(path.split('/')))}
    pending = [()]
    while pending:
        prefix = pending.pop()
        for name in list_names_beneath(root, (*parts, *prefix)):
            relative = (*prefix, name)
            key = '/'.join(relative)
            if key in expected:
                raw = read_bytes_beneath(root, (*parts, *relative), require_dir_fd=True)
                if raw is None or hashlib.sha256(raw).hexdigest() != expected[key]:
                    raise SkillTransferError('SKILL_TRANSFER_OPERATION_CONFLICT')
            elif relative in parents:
                pending.append(relative)
            else:
                raise SkillTransferError('SKILL_TRANSFER_OPERATION_CONFLICT')


# LLM: 去重从原skills可信根复核实际资源字节，不能只因registry摘要相同或子目录名相同就跟随父链接。
# 函数用途: 检查已存在导入的显式资源是否仍与授权清单一致，缺失或修改要求新确认。
def _resources_current(root, directory_parts, item):
    for path, record in item['files'].items():
        if path == 'SKILL.md':
            continue
        raw = read_bytes_beneath(root, (*directory_parts, *resource_parts(path)), require_dir_fd=True)
        if raw is None or hashlib.sha256(raw).hexdigest() != record['sha256']:
            return False
    return True
