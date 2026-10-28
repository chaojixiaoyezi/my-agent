# LLM: 四类自动提炼资产共用原registry；不授工具、Owner或人格修改权限。
# 模块用途: 为学习入口统一语义族、来源与机器验证晋升。
from __future__ import annotations

import ast
import hashlib
import json
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


# LLM: 只计算语义族，不作为授权或验证通过证明。
# 函数用途: 计算资产方法的稳定去重键。
def task_family_fingerprint(kind, asset):
    if kind == 'script':
        code = asset.get('entry_source', asset.get('code', ''))
        signature = [ast.dump(ast.parse(code), include_attributes=False), asset.get('input_schema'), asset.get('output_schema')]
    elif kind == 'workflow':
        steps = asset.get('steps', [])
        ids = {step.get('step_id', step.get('id')): str(i) for i, step in enumerate(steps)}
        signature = [{key:value for key,value in step.items() if key not in {'name','step_id','id'}} for step in steps]
        signature = _normalize_step_refs(signature, ids)
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


# LLM: 调用方持原state/配额锁；只保存摘要/精确引用不存代码秘密，语义族不替代正式hash和CAS。
# 函数用途: 在唯一registry内合并四类资产来源，不因改名重复创建同族资产。
def register_asset_locked(store, registry, *, kind, asset_id, asset, source, sha256='', state='candidate'):
    family = task_family_fingerprint(kind, asset)
    previous = registry.assets.get(family, {})
    refs = list(previous.get('provenance', []))
    if source not in refs:
        refs.append(source)
    same = previous.get('asset_id', asset_id).removeprefix('owner:') == asset_id.removeprefix('owner:')
    update = same and bool(sha256)
    row = {**previous, 'kind':kind, 'asset_id':previous.get('asset_id', asset_id),
           'task_family_fingerprint':family, 'sha256':sha256 if update else previous.get('sha256', ''),
           'state':state if update or not previous else previous['state'], 'provenance':refs[-10:]}
    return replace(registry, assets={**registry.assets, family:row}), row


# LLM: 仅宿主调用；原Owner锁包住唯一registry登记，正文仍留原资产库，不接管用户文件。
# 函数用途: 为S1与显式提炼登记同族来源，返回可复用的既有资产身份。
def register_asset(store, **kwargs):
    with store.state_lock():
        registry, row = register_asset_locked(store, store.load_registry(), **kwargs)
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


# LLM: 隔离暂存目录并保护手工维护文件，任何在暂存区内写出AGENTS/SOUL立即拦截并抛MANUAL_ONLY_TARGET，绝不覆写Owner真文件。
# 函数用途: 为候选脚本和工作流提供与正式Owner隔离的验证环境。
@contextmanager
def _stage_dir(home):
    import shutil
    from uuid import uuid4

    from .skill_learning_publish import SkillLearningGateError

    staging = home / 'data' / 'skill_learning' / ('_stage_' + uuid4().hex)
    staging.mkdir(parents=True, exist_ok=True)
    for folder in ('scripts', 'skills'):
        if (home / folder).is_dir():
            shutil.copytree(home / folder, staging / folder)
    try:
        yield staging
        if any((staging / name).exists() for name in ('AGENTS.md', 'SOUL.md')):
            raise SkillLearningGateError('MANUAL_ONLY_TARGET')
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# LLM: 先在隔离暂存区开启restrict_to_roots沙箱执行并核对业务合同，通过后才按CAS写入正式ScriptStore。
# 函数用途: 验证并发布脚本候选，失败绝不覆盖或删除既有正式脚本。
def _promote_script_asset(home, name, desc, candidate):
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
    with _stage_dir(home) as staging:
        ScriptStore(staging).save_script(desc_obj, code)
        res = ScriptRunner(staging, use_sandbox=True, restrict_to_roots=True).run_script(name, candidate.get('sample_inputs', {}))
        out = getattr(res, 'parsed_output', None)
        receipt = {'status': 'passed' if res.ok else 'failed', 'executed': True,
                   'contract_valid': bool(res.ok), 'output': out if isinstance(out, dict) else {}}
        if not business_validated(receipt):
            return False
    try:
        sstore.save_script(desc_obj, code, expected_revision=target_rev)
    except ValueError:
        return False
    return sstore.get_script(name) is not None


# LLM: 工作流在隔离暂存区执行真实步骤（禁止注入假analysis_handler），通过后才按CAS写入正式WorkflowStore。
# 函数用途: 验证并发布工作流候选，失败绝不污染正式定义或运行记录。
def _promote_workflow_asset(home, name, desc, candidate):
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
    with _stage_dir(home) as staging:
        WorkflowStore(staging).save_workflow(defn)
        eng = WorkflowEngine(staging, analysis_handler=candidate.get('analysis_handler'))
        eng.runner = ScriptRunner(staging, use_sandbox=True, restrict_to_roots=True)
        run = eng.run_workflow(defn, inputs=candidate.get('sample_inputs', {}))
        receipt = {'status': run.status, 'executed': True, 'contract_valid': run.status == 'succeeded',
                   'output': run.final_output if isinstance(run.final_output, dict) else {}}
        if not business_validated(receipt):
            return False
    try:
        wstore.save_workflow(defn, expected_revision=target_rev)
    except ValueError:
        return False
    return wstore.get_workflow(name) is not None


# LLM: 能力包按正式plugin_package.v7归档检验并写入PluginInstallStore且激活，支持list_installed_capability_packages读取。
# 函数用途: 构建、核验、安装并启用纯内容能力包，失败不覆盖既有安装。
def _promote_package_asset(home, name, desc, candidate):
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
    from ..user_space.owner_resolver import resolve_owner_home
    from .package_provider import _owner_from_home, list_installed_capability_packages

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
    try:
        owner = _owner_from_home(home)
    except ValueError:
        owner = resolve_owner_home(home)
    pstore = PluginInstallStore(owner)
    existing = next((item for item in pstore.snapshot() if item.manifest.plugin_id == name), None)
    if existing is not None and candidate.get('expected_revision') != existing.revision:
        return False
    inst = pstore.install(PluginInstallRequest(snap, f'promote:{name}:{existing.revision if existing else 0}', existing.revision if existing else 0)).installation
    act = PluginContentActivation('enable', inst.manifest.plugin_id, inst.package_sha256, inst.revision, inst.settings_revision)
    pstore.change_activation(PluginActivationRequest('enable', inst.revision, act))
    return any(r['package_id'] == name and r['enabled'] for r in list_installed_capability_packages(owner.home_dir, include_disabled=False))


# LLM: 四类载体统一经静态安全门与隔离暂存/安装核验；AGENTS/SOUL硬拦截，诚实区分静态检查与真实执行证明。
# 函数用途: 按载体类型自动完成Skill/Script/Workflow/CapabilityPackage发布、回读与验证并写入唯一registry。
def promote_verified_asset(owner_home, candidate):
    from pathlib import Path

    from .skill_learning_prompt import SkillLearningDecision
    from .skill_learning_publish import PublishRequest, SkillLearningGateError

    home = Path(owner_home).absolute()
    store = SkillLearningStore(home / 'data' / 'skill_learning', home / 'skills')
    static = default_machine_promotion_verifier(candidate)
    if static.get('error_code') in {'MANUAL_ONLY_TARGET', 'MACHINE_DRAFT_NAME_INVALID'}:
        raise SkillLearningGateError(static['error_code'])
    if static.get('status') != 'passed':
        return None
    carrier, name, desc = select_asset_carrier(candidate), candidate['name'], candidate['description']
    if carrier == 'skill':
        dec = SkillLearningDecision('create', 'verified', name=name, description=desc,
            when_to_use=candidate.get('when_to_use', desc), tags=tuple(candidate.get('tags', ())), body=candidate['body'])
        event = promote_verified_skill(store, PublishRequest(dec), candidate.get('verifier'))
        if event is None:
            return None
        rec = store.load_registry().skills.get(event.skill_name or name)
        v_rec = (rec.maintenance.get('verification_receipt') or {}) if rec else {}
        exec_ok = v_rec.get('executed') is True
        return {'carrier': 'skill', 'asset_id': event.skill_name or name, 'verification_kind': 'runtime_execution' if exec_ok else 'deterministic_static',
                'checks_executed': True, 'executed': exec_ok, 'contract_valid': True, 'sha256': static['sha256']}
    prev = store.load_registry().assets.get(task_family_fingerprint(carrier, candidate))
    if prev and prev.get('asset_id') and prev['asset_id'].removeprefix('owner:') != name.removeprefix('owner:'):
        row = register_asset(store, kind=carrier, asset_id=name, asset=dict(candidate),
                             source={'origin': 'autonomous_verified', 'receipt_id': static['receipt_id']},
                             sha256=static['sha256'], state='trial')
        return {'carrier': carrier, 'asset_id': row['asset_id'], 'reused': True, 'verification_kind': 'deterministic_static',
                'checks_executed': True, 'executed': False, 'contract_valid': True, 'sha256': static['sha256']}
    handlers = {'script': _promote_script_asset, 'workflow': _promote_workflow_asset, 'capability_package': _promote_package_asset}
    if not handlers[carrier](home, name, desc, candidate):
        return None
    executed = carrier in {'script', 'workflow'}
    row = register_asset(store, kind=carrier, asset_id=name, asset=dict(candidate),
                         source={'origin': 'autonomous_verified', 'receipt_id': static['receipt_id']},
                         sha256=static['sha256'], state='trial')
    return {'carrier': carrier, 'asset_id': row['asset_id'], 'verification_kind': 'runtime_execution' if executed else 'deterministic_static',
            'checks_executed': True, 'executed': executed, 'contract_valid': True, 'sha256': static['sha256']}
