# LLM: 四类自动提炼资产共用原registry；不授工具、Owner或人格修改权限。
# 模块用途: 为学习入口统一语义族、来源与机器验证晋升。
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from contextlib import ExitStack, contextmanager
from dataclasses import replace

from ..common.nofollow_fs import read_text_beneath, unlink_file_beneath, write_text_atomic_beneath
from .skill_learning_promotion import promote_verified_skill as promote_verified_skill
from .skill_learning_promotion import propose_or_promote_skill as propose_or_promote_skill
from .skill_learning_store import LearnedSkill, SkillLearningStore, utc_now
from .skill_learning_validation import (
    default_machine_promotion_verifier as default_machine_promotion_verifier,
)
from .skill_snapshot import skill_content_sha256


# LLM: 纯结构分类，显式四类载体优先，否则按分支/步骤/命令归类；不解析自然语言授执行权。
# 函数用途: 选择适合复用的资产载体。
def select_asset_carrier(features):
    nested = features.get('candidate_asset')
    data = {**features, **nested} if isinstance(nested, dict) and nested else features
    if data.get('kind') in {'skill','script','workflow','capability_package'}:
        return data['kind']
    if data.get('branches'):
        return 'skill'
    if len(data.get('steps') or ()) > 1:
        return 'workflow'
    return 'script' if (data.get('commands') or data.get('code') or data.get('entry_source')) else 'skill'


# LLM: 归一化非空IO合同字段，消除候选字典与AssetDescriptor默认空字段的形态差异。
# 函数用途: 提取资产IO合同的非空签名结构。
def _norm_io(raw):
    if not isinstance(raw, dict):
        return None
    cleaned = {k: list(v) if isinstance(v, (list, tuple)) else v for k, v in raw.items() if v not in ('', [], (), {}, None)}
    return cleaned or None


# LLM: 只计算语义族，不作为授权或验证通过证明。
# 函数用途: 计算资产方法的稳定去重键。
def task_family_fingerprint(kind, asset):
    io_sig = _norm_io(asset.get('io_contract'))
    if kind == 'script':
        code = str(asset.get('entry_source', asset.get('code', '')) or '')
        try:
            ast_sig = ast.dump(ast.parse(code), include_attributes=False)
        except SyntaxError:
            ast_sig = f'syntax_error:{code}'
        signature = [ast_sig, asset.get('input_schema'), asset.get('output_schema')]
        if io_sig:
            signature.append(io_sig)
    elif kind == 'workflow':
        steps = asset.get('steps', [])
        ids = {step.get('step_id', step.get('id')): str(i) for i, step in enumerate(steps)}
        signature = [{key:value for key,value in step.items() if key not in {'name','step_id','id'} and value not in ('', [], (), {}, None, 'escalate_to_agent', 120, 120.0)} for step in steps]
        signature = _normalize_step_refs(signature, ids)
        if io_sig:
            signature = [signature, io_sig]
    else:
        body = asset.get('body', '')
        body = re.sub(r'\A---\n.*?\n---\n', '', body, flags=re.S)
        body = re.sub(r'^#{1,6}[^\n]*', '', body, flags=re.M)
        signature = re.findall(r'[\w]+', body.casefold())
    return hashlib.sha256(json.dumps([kind, signature], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# LLM: 只规范明确的DAG引用字段，业务字面值与未知字段原样；映射一次不级联重写。
# 函数用途: 对步骤重命名消歧，不把不同业务输入或不同依赖合为同族。
def _normalize_step_refs(value, ids):
    if isinstance(value, list):
        return [_normalize_step_refs(item, ids) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: [ids.get(item, item) for item in val] if key == 'depends_on' and isinstance(val, list)
            else ids.get(val, val) if key in {'step_id', 'rule_id'} and isinstance(val, str)
            else _normalize_step_refs(val, ids) for key, val in value.items()}


# LLM: 不把退出成功等同业务执行通过。
# 函数用途: 检查资产真实执行与业务验证证据。
def business_validated(receipt):
    output = receipt.get('output', {})
    return (receipt.get('status') in {'passed', 'succeeded'} and receipt.get('executed') is True
            and receipt.get('contract_valid') is True and isinstance(output, dict)
            and output.get('dry_run') is not True and output.get('executed') is not False)


# LLM: 绑定来自提交返回的原记录，而不是事后读到的新版本；显示投影不参与身份，原revision和全部定义字段参与。
# 函数用途: 给提交结果和当前读回共用同一正式版本摘要。
def _binding_from_record(kind, record):
    if not record:
        return None
    if kind == 'script':
        from dataclasses import fields

        from ..assets.descriptor import AssetDescriptor
        if record.get('enabled', True) is False:
            return None
        keys = {item.name for item in fields(AssetDescriptor)} | {'entrypoint', 'interpreter', 'timeout_seconds', 'code'}
        payload = {key: record[key] for key in keys if key in record}
    elif kind == 'workflow':
        if not record.enabled:
            return None
        payload = record.to_dict()
    else:
        return record.package_sha256 + ':' + record.activation_id if record.enabled else None
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# LLM: 只读当前原权威版本，缺失/停用不授权；不将事后读回值作为候选验证的期望版本。
# 函数用途: 重核正式版本是否仍等于这次准确提交返回的binding。
def _formal_binding(home, kind, name):
    if kind == 'script':
        from ..assets.script_store import ScriptStore
        return _binding_from_record(kind, ScriptStore(home).get_script(name))
    if kind == 'workflow':
        from ..assets.workflow_store import WorkflowStore
        return _binding_from_record(kind, WorkflowStore(home).get_workflow(name))
    from ..plugin_install_store import PluginInstallStore
    from .package_provider import _owner_from_home
    rows = PluginInstallStore(_owner_from_home(home)).snapshot()
    return next((_binding_from_record(kind, entry) for entry in rows
                 if entry.manifest.plugin_id == name and entry.enabled and entry.installation_scope == 'private'), None)


# LLM: 精确身份绑定原owner、规范ID、成员、依赖、版本、说明、合同和样本输入；仅原因/callback/CAS前置不参与，family只供推荐。
# 函数用途: 为候选资产计算精确版本摘要，不以正文相似性代替内容身份。
def asset_identity(kind, asset, owner_home):
    ignored = {'reason', 'verifier', 'analysis_handler', 'expected_revision'}
    values = {key: value for key, value in asset.items() if key not in ignored}
    return hashlib.sha256(json.dumps([str(owner_home), kind, values], sort_keys=True,
        ensure_ascii=False, default=_identity_json_value).encode()).hexdigest()


# LLM: 身份只接受JSON和二进制成员；不串行化callback地址或未知对象的repr。
# 函数用途: 用原始字节hash表达二进制成员，其余不支持类型明确拒绝。
def _identity_json_value(value):
    if isinstance(value, bytes):
        return {'bytes_sha256': hashlib.sha256(value).hexdigest()}
    raise TypeError('asset identity is not JSON')


# LLM: 调用方持原state/配额锁，按精确身份存储；旧family行仅保历史，不签复用或选择旧asset_id。
# 函数用途: 在唯一registry保留不同ID、成员和scope的资产，合并同版本出处。
def register_asset_locked(store, registry, *, kind, asset_id, asset, source, sha256='', state='candidate'):
    family = task_family_fingerprint(kind, asset)
    identity = asset_identity(kind, {**asset, 'name': asset_id.removeprefix('owner:')}, store.skills_root.parent)
    previous = registry.assets.get(identity, {})
    clean_src = {k: v for k, v in source.items() if k != 'force_replace'} if isinstance(source, dict) else source
    refs = list(previous.get('provenance', []))
    if clean_src not in refs:
        refs.append(clean_src)
    row = {**previous, 'kind':kind, 'asset_id':asset_id, 'identity_sha256':identity,
           'task_family_fingerprint':family, 'sha256':sha256 or previous.get('sha256', ''),
           'state':state, 'provenance':refs[-10:]}
    return replace(registry, assets={**registry.assets, identity:row}), row


# LLM: 仅宿主调用；原Owner锁包住唯一registry登记，正文仍留原库；登记事实本身不签正式版本复用权。
# 函数用途: 为S1与提炼保存精确资产版本出处，不把同族推荐变成授权证明。
def register_asset(
    store,
    *,
    kind,
    asset_id,
    asset,
    source,
    sha256='',
    state='candidate',
    options=None,
):
    _ = options
    with store.state_lock():
        registry, row = register_asset_locked(
            store,
            store.load_registry(),
            kind=kind,
            asset_id=asset_id,
            asset=asset,
            source=source,
            sha256=sha256,
            state=state,
        )
        store.save_registry(registry)
    return row


# LLM: 提炼事务持原资产锁；yield跨正文/提案写入，失败恢复准确版本及registry，不接管workspace正文。
# 函数用途: 把显式发布的资产来源和Owner技能纳入同一维护权威。
@contextmanager
def register_extracted_assets(home, chosen, proposal, scope):
    from pathlib import Path

    store = SkillLearningStore(Path(home)/'data'/'skill_learning', Path(home)/'skills')
    with store.state_lock(), ExitStack() as rollback:
        registry = store.load_registry()
        _restore_on_failure(store, ('registry.json',), rollback)
        for kind, asset in chosen:
            identifier = asset['descriptor']['id'] if kind == 'workflow' else asset.get('script_id', asset.get('name'))
            text = asset.get('body', asset.get('entry_source', json.dumps(asset, sort_keys=True)))
            sha = skill_content_sha256(text)
            registry, _ = register_asset_locked(store, registry, kind=kind, asset_id=identifier, asset=asset,
                source={'origin':'turn_extractor','proposal_id':proposal['proposal_id'],'scope':scope}, sha256=sha, state='trial')
            if kind == 'skill' and scope == 'owner':
                if identifier in registry.skills or identifier in registry.blocked_names:
                    raise ValueError('EXTRACTION_REGISTERED_NAME_EXISTS')
                now = utc_now()
                _restore_on_failure(store, ('versions', identifier, 'v1.md'), rollback)
                store.save_version(identifier, 1, text)
                record = LearnedSkill(identifier, 1, sha, now, now, maintenance={'state':'trial','quality':'trial'})
                registry = replace(registry, skills={**registry.skills, identifier:record})
        yield
        store.save_registry(registry)
        rollback.pop_all()


# LLM: 恢复只作用于原store目录下的准确叶，不遍历、不删除既有目录；异常向上传递不能伪报成功。
# 函数用途: 为注册事务保存写入前的字节状态。
def _restore_on_failure(store, parts, rollback):
    previous = read_text_beneath(store.directory, parts)
    rollback.callback(unlink_file_beneath, store.directory, parts) if previous is None else rollback.callback(write_text_atomic_beneath, store.directory, parts, previous)


# LLM: 仅从步骤明确引用的脚本取元数据与入口；逐段nofollow拒绝link/hardlink，缺失及损坏不回退。
# 函数用途: 为工作流沙箱准备最小脚本依赖文件清单，不复制无关脚本或整个技能树。
def _workflow_dependencies(home, steps):
    from ..assets import safe_asset_id
    from .skill_learning_publish import SkillLearningGateError

    dependencies = []
    for step in steps:
        script_id = step.get('script_id')
        if script_id:
            safe_asset_id(script_id)
            text = read_text_beneath(home, ('scripts', script_id, 'script.json'))
            if text is None:
                raise SkillLearningGateError('LEARNING_DEPENDENCY_MISSING')
            entry = json.loads(text)['entrypoint']
            safe_asset_id(entry)
            dependencies.extend((('scripts', script_id, 'script.json'), ('scripts', script_id, entry)))
        if step.get('skill_ids'):
            # A text method with executable side resources needs a declared frozen bundle, not a guessed whole-library copy.
            raise SkillLearningGateError('LEARNING_SKILL_DEPENDENCY_UNFROZEN')
    return tuple(dict.fromkeys(dependencies))


# LLM: 清单只取明确依赖，包含真实文件字节hash且有读上限；调用者不能用自报hash替代宿主读取。
# 函数用途: 冻结工作流依赖身份，供staging复制和最终Commit双向核对。
def _dependency_hashes(home, steps):
    from ..common.nofollow_fs import read_bytes_beneath
    result = {}
    for parts in _workflow_dependencies(home, steps):
        raw = read_bytes_beneath(home, parts, max_bytes=4 * 1024 * 1024, require_dir_fd=True)
        if raw is None:
            raise OSError('learning dependency disappeared')
        result['/'.join(parts)] = hashlib.sha256(raw).hexdigest()
    return result


# LLM: 可信parent FD和准确stage inode贯穿生命周期；strict nofollow只复制显式依赖，失败清理或登记原对象，不删除替代目录。
# 函数用途: 创建有预算的隔离执行区，准确回收本次目录并保留清理失败/登记失败的独立诊断。
@contextmanager
def _stage_dir(home, *, dependencies=(), dependency_hashes=None):
    import logging
    import shutil
    import stat
    import sys
    from uuid import uuid4

    from ..common.nofollow_fs import (
        open_directory_beneath,
        read_bytes_beneath,
        write_bytes_atomic_beneath,
    )
    from .skill_learning_publish import SkillLearningGateError

    staging = home / 'data' / 'skill_learning' / ('_stage_' + uuid4().hex)
    parent_fd, identity = None, None
    try:
        parent_fd = open_directory_beneath(home, ('data', 'skill_learning'), create=True, require_dir_fd=True)
        os.mkdir(staging.name, mode=0o700, dir_fd=parent_fd)
        created = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
        identity = (created.st_dev, created.st_ino)
        total = count = 0
        for parts in dependencies:
            if not isinstance(parts, tuple) or not parts or parts[0] not in {'scripts', 'skills'}:
                raise SkillLearningGateError('LEARNING_DEPENDENCY_INVALID')
            raw = read_bytes_beneath(home, parts, max_bytes=4 * 1024 * 1024, require_dir_fd=True)
            if raw is None:
                raise SkillLearningGateError('LEARNING_DEPENDENCY_MISSING')
            if dependency_hashes is not None and dependency_hashes.get('/'.join(parts)) != hashlib.sha256(raw).hexdigest():
                raise SkillLearningGateError('LEARNING_DEPENDENCY_CHANGED')
            count, total = count + 1, total + len(raw)
            if count > 256 or total > 16 * 1024 * 1024:
                raise SkillLearningGateError('LEARNING_STAGING_BUDGET_EXCEEDED')
            _require_stage_identity(home, staging, identity)
            write_bytes_atomic_beneath(home, (*staging.relative_to(home).parts, *parts), raw)
        _require_stage_identity(home, staging, identity)
        yield staging
        _require_stage_identity(home, staging, identity)
        if any((staging / name).exists() or (staging / name).is_symlink() for name in ('AGENTS.md', 'SOUL.md')):
            raise SkillLearningGateError('MANUAL_ONLY_TARGET')
    finally:
        primary = sys.exception()
        try:
            if identity is not None:
                try:
                    current = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
                    same = stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == identity
                except FileNotFoundError:
                    same = False
                if not same:
                    raise SkillLearningGateError('LEARNING_STAGING_IDENTITY_CHANGED')
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise SkillLearningGateError('LEARNING_CLEANUP_UNSUPPORTED')
                shutil.rmtree(staging.name, dir_fd=parent_fd)
        except (OSError, SkillLearningGateError) as cleanup_error:
            try:
                write_text_atomic_beneath(home, ('data', 'skill_learning', 'cleanup_pending', staging.name + '.json'),
                    json.dumps({'schema_version': 1, 'stage': staging.name, 'identity': identity, 'status': 'cleanup_pending'}))
            except (OSError, ValueError) as receipt_error:
                cleanup_error.add_note('cleanup_pending_write_failed:' + type(receipt_error).__name__)
                logging.getLogger(__name__).error('LEARNING_CLEANUP_PENDING_WRITE_FAILED stage=%s identity=%s', staging.name, identity)
            if primary is not None:
                primary.add_note('learning_cleanup_failed:' + staging.name)
                raise primary from cleanup_error
            raise
        finally:
            if parent_fd is not None:
                os.close(parent_fd)


# LLM: 逐段strict nofollow重开当前路径，只比对本次创建inode，不沿链接或用新目录替换原对象。
# 函数用途: 在复制和执行边界确认staging路径仍指向本次目录。
def _require_stage_identity(home, staging, identity):
    from ..common.nofollow_fs import open_directory_beneath
    from .skill_learning_publish import SkillLearningGateError
    descriptor = open_directory_beneath(home, staging.relative_to(home).parts, require_dir_fd=True)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != identity:
            raise SkillLearningGateError('LEARNING_STAGING_IDENTITY_CHANGED')
    finally:
        os.close(descriptor)


# LLM: 先在隔离暂存区开启restrict_to_roots沙箱执行并核对业务合同，通过后才按CAS写入正式ScriptStore。
# 函数用途: 验证并发布脚本候选，失败绝不覆盖或删除既有正式脚本。
def _promote_script_asset(home, name, desc, candidate, *, commit=None):
    from ..assets.descriptor import AssetDescriptor
    from ..assets.script_store import ScriptStore
    from ..workflows import ScriptRunner

    sstore = ScriptStore(home)
    existing = sstore.get_script(name)
    if existing is not None and candidate.get('expected_revision') != existing.get('revision'):
        return False
    target_rev = existing.get('revision') if existing is not None else 0
    code = candidate.get('entry_source', candidate.get('code', ''))
    desc_obj = AssetDescriptor.from_dict({'id': name, 'kind': 'script', 'name': name, 'summary': desc,
                                          'io_contract': candidate.get('io_contract', {})})
    options = {key: candidate[key] for key in ('entrypoint', 'interpreter', 'timeout_seconds') if key in candidate}
    with _stage_dir(home) as staging:
        ScriptStore(staging).save_script(desc_obj, code, **options)
        res = ScriptRunner(staging, use_sandbox=True, restrict_to_roots=True).run_script(name, candidate.get('sample_inputs', {}))
        out = getattr(res, 'output_data', None)
        if out is None:
            out = getattr(res, 'parsed_output', None)
        receipt = {'status': 'passed' if res.ok else 'failed', 'executed': True,
                   'contract_valid': bool(res.ok), 'output': out if isinstance(out, dict) else {}}
        if not business_validated(receipt):
            return False
    try:
        from .skill_learning_promotion import _commit
        submitted = _commit(commit, lambda: sstore.save_script(desc_obj, code, expected_revision=target_rev, **options))
    except ValueError:
        return False
    return _binding_from_record('script', submitted)


# LLM: 工作流在隔离暂存区执行真实步骤（禁止注入假analysis_handler），通过后才按CAS写入正式WorkflowStore。
# 函数用途: 验证并发布工作流候选，失败绝不污染正式定义或运行记录。
def _promote_workflow_asset(home, name, desc, candidate, *, commit=None):
    from ..assets.workflow_store import WorkflowDefinition, WorkflowStore
    from ..workflows import ScriptRunner, WorkflowEngine

    wstore = WorkflowStore(home)
    existing = wstore.get_workflow(name)
    if existing is not None and candidate.get('expected_revision') != existing.descriptor.revision:
        return False
    target_rev = existing.descriptor.revision if existing is not None else 0
    defn = WorkflowDefinition.from_any({
        'descriptor': {'id': name, 'kind': 'workflow', 'name': name, 'summary': desc, 'io_contract': candidate.get('io_contract', {})},
        'steps': candidate['steps'], 'body_markdown': candidate.get('body_markdown', ''),
    })
    dependencies = _workflow_dependencies(home, candidate['steps'])
    with _stage_dir(home, dependencies=dependencies, dependency_hashes=candidate['_dependency_hashes']) as staging:
        WorkflowStore(staging).save_workflow(defn)
        eng = WorkflowEngine(staging)
        eng.runner = ScriptRunner(staging, use_sandbox=True, restrict_to_roots=True)
        run = eng.run_workflow(defn, inputs=candidate.get('sample_inputs', {}))
        receipt = {'status': run.status, 'executed': True, 'contract_valid': run.status == 'succeeded',
                   'output': run.final_output if isinstance(run.final_output, dict) else {}}
        if not business_validated(receipt):
            return False
    try:
        from .skill_learning_promotion import _commit
        # LLM: 仅本次栈内Commit重核冻结依赖，不能用同script_id替代准确成员hash。
        # 函数用途: 在最后写门内确认验证依赖仍一致并CAS提交。
        def save():
            if _dependency_hashes(home, candidate['steps']) != candidate['_dependency_hashes']:
                from .skill_learning_publish import SkillLearningGateError
                raise SkillLearningGateError('LEARNING_DEPENDENCY_CHANGED')
            return wstore.save_workflow(defn, expected_revision=target_rev)
        submitted = _commit(commit, save)
    except ValueError:
        return False
    return _binding_from_record('workflow', submitted)


# LLM: 纯内容包先完整检验；安装/启用同一次原来源短门但底层各CAS，失败可能保留禁用安装，不宣称零副作用。
# 函数用途: 安全构建发布能力包并返回准确启用代次，原安装表仍是唯一状态权威。
def _promote_package_asset(home, name, desc, candidate, *, commit=None):
    import io
    import zipfile

    from ..plugin_activation import PluginActivationRequest
    from ..plugin_content_activation import PluginContentActivation
    from ..plugin_install_store import PluginInstallStore
    from ..plugin_installation import PluginInstallRequest
    from ..plugin_package import (
        PackageReadLimits,
        inspect_plugin_package,
        read_plugin_member,
    )
    from .package_provider import _owner_from_home

    entry_doc = candidate.get('entry_document', 'CAPABILITY.md')
    raw_files = {entry_doc: str(candidate.get('body', desc)), **dict(candidate.get('files') or {})}
    encoded = {p: (v if isinstance(v, bytes) else str(v).encode('utf-8')) for p, v in raw_files.items()}
    cap = {'description': desc, 'keywords': list(candidate.get('keywords') or candidate.get('tags') or [name]), 'entry_document': entry_doc}
    if candidate.get('application_scope'):
        cap['application_scope'] = candidate['application_scope']
    manifest = {'schema_version': 'plugin_package.v7', 'package_kind': 'capability', 'plugin_id': name,
                'version': str(candidate.get('version', '1.0.0')), 'summary': desc, 'capability': cap,
                'files': [{'path': p, 'sha256': hashlib.sha256(b).hexdigest(), 'executable': False} for p, b in encoded.items()],
                'settings_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('plugin.json', json.dumps(manifest, ensure_ascii=False))
        for p, b in encoded.items():
            zf.writestr(p, b)
    snap = inspect_plugin_package(buf.getvalue())
    if not read_plugin_member(snap, entry_doc, max_bytes=PackageReadLimits().member_bytes):
        return False
    owner = _owner_from_home(home)
    pstore = PluginInstallStore(owner)
    existing = next((item for item in pstore.snapshot() if item.manifest.plugin_id == name), None)
    if existing is not None and candidate.get('expected_revision') != existing.revision:
        return False
    from .skill_learning_promotion import _commit
    # LLM: 安装与启用共用一次原来源短门；底层安装表仍是两次CAS，启用失败可留已安装但禁用的准确revision，不伪报零写入。
    # 函数用途: 在本次原Commit内完成已验证内容包发布，返回准确启用记录用于receipt。
    def install_and_enable():
        inst = pstore.install(PluginInstallRequest(snap, f'promote:{name}:{existing.revision if existing else 0}', existing.revision if existing else 0)).installation
        act = PluginContentActivation('enable', inst.manifest.plugin_id, inst.package_sha256, inst.revision, inst.settings_revision)
        return pstore.change_activation(PluginActivationRequest('enable', inst.revision, act)).installation

    enabled = _commit(commit, install_and_enable)
    return _binding_from_record('capability_package', enabled)


# LLM: Prepare/Verify不占原C短门；精确identity+正式binding才复用，发布/登记分别短Commit，取消/撤权不授写。
# 函数用途: 四载体经过隔离验证与CAS后发布，保留真实receipt及正式版本绑定，绝不family-only复用。
def promote_verified_asset(owner_home, candidate, *, commit=None):
    from copy import deepcopy
    from pathlib import Path

    from .skill_learning_promotion import _commit
    from .skill_learning_prompt import SkillLearningDecision
    from .skill_learning_publish import PublishRequest, SkillLearningGateError

    candidate = deepcopy(candidate)
    home = Path(owner_home).absolute()
    store = SkillLearningStore(home / 'data' / 'skill_learning', home / 'skills')
    if select_asset_carrier(candidate) == 'workflow':
        candidate['_dependency_hashes'] = _dependency_hashes(home, candidate.get('steps', []))
    static = default_machine_promotion_verifier(candidate)
    if static.get('error_code') in {'MANUAL_ONLY_TARGET', 'MACHINE_DRAFT_NAME_INVALID'}:
        raise SkillLearningGateError(static['error_code'])
    if static.get('status') != 'passed':
        return None
    carrier, name, desc = select_asset_carrier(candidate), candidate['name'], candidate['description']
    if carrier == 'skill':
        dec = SkillLearningDecision('create', 'verified', name=name, description=desc,
            when_to_use=candidate.get('when_to_use', desc), tags=tuple(candidate.get('tags', ())), body=candidate['body'])
        event = promote_verified_skill(store, PublishRequest(dec), candidate.get('verifier'), commit=commit)
        if event is None:
            return None
        rec = store.load_registry().skills.get(event.skill_name or name)
        return {'carrier': 'skill', 'asset_id': event.skill_name or name, **rec.maintenance['verification_receipt']}
    identity = asset_identity(carrier, candidate, home)
    previous = store.load_registry().assets.get(identity, {})
    binding = _formal_binding(home, carrier, name)
    if (previous.get('state') in {'trial', 'trusted'} and previous.get('identity_sha256') == identity
            and binding and previous.get('formal_binding') == binding):
        return {**previous['verification_receipt'], 'asset_id': name, 'reused': True}
    handlers = {'script': _promote_script_asset, 'workflow': _promote_workflow_asset, 'capability_package': _promote_package_asset}
    binding = handlers[carrier](home, name, desc, candidate, commit=commit)
    if not binding:
        return None
    if not isinstance(binding, str) or _formal_binding(home, carrier, name) != binding:
        raise SkillLearningGateError('LEARNING_FORMAL_READBACK_FAILED')
    executed = carrier in {'script', 'workflow'}
    receipt = {'carrier': carrier, 'asset_id': name, 'verification_kind': 'runtime_execution' if executed else 'deterministic_static',
               'checks_executed': True, 'executed': executed, 'contract_valid': True, 'sha256': static['sha256'],
               'identity_sha256': identity, 'formal_binding': binding}
    # LLM: 登记时重核正式版本，避免验证后并发替换导致receipt绑定到未验证的新版本。
    # 函数用途: 短Commit里登记准确发布结果，不以晚到回执覆盖当前版本。
    def record():
        with store.state_lock():
            if carrier == 'workflow' and _dependency_hashes(home, candidate['steps']) != candidate['_dependency_hashes']:
                raise SkillLearningGateError('LEARNING_DEPENDENCY_CHANGED')
            if _formal_binding(home, carrier, name) != binding:
                raise SkillLearningGateError('LEARNING_FORMAL_REVISION_CHANGED')
            registry, row = register_asset_locked(store, store.load_registry(), kind=carrier, asset_id=name, asset=candidate,
                source={'origin': 'autonomous_verified', 'receipt_id': static['receipt_id']}, sha256=static['sha256'], state='trial')
            row = {**row, 'verification_receipt': receipt, 'formal_binding': binding}
            store.save_registry(replace(registry, assets={**registry.assets, identity: row}))
    _commit(commit, record)
    return receipt
