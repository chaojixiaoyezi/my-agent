"""显式双端Skill迁移、一次成功复刻、资源/秘密/容量与故障恢复的原账测试。"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_transfer, skill_transfer_import
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_transfer_control import execute_transfer_control
from agent_py_agent.agent.capability.skill_transfer_manifest import SkillTransferError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.skill_transfer_tool import SkillTransferTool
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture
def transfer_case(tmp_path,monkeypatch):
    def agent(owner):
        return SimpleAgent(AgentConfig(model_backend='echo',my_agent_home=str(tmp_path/'home'),enable_plugins=False,
            my_agent_owner_provider=owner.provider,my_agent_owner_kind=owner.owner_kind,my_agent_owner_id=owner.owner_id),tmp_path)
    source=agent(OwnerIdentity.provider_user('feishu','ou_source'))
    target=agent(OwnerIdentity.provider_group('feishu','oc_target'))
    other=agent(OwnerIdentity.provider_group('feishu','oc_other'))
    monkeypatch.setattr(skill_transfer,'read_fresh_capability_switch',lambda *args:True)
    monkeypatch.setattr('agent_py_agent.agent.scheduler.creator_access.current_group_members',lambda host,chat:{'ou_receiver'})
    agents={tuple(asdict(OwnerIdentity(home.home_paths.owner_provider,home.home_paths.owner_kind,home.home_paths.owner_id.split('/')[-1])).values()):home for home in (source,target,other)}
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.control_service._request_agent_for_scope',
        lambda base,scope:agents[tuple(asdict(scope.resolved_owner).values())])
    path=source.home_paths.owner_home_dir/'skills'/'sample'/'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: sample\ndescription: 安全求和方法\n---\n\n先校验数据，再相加。\n')
    resource=path.parent/'references'/'sample.txt'
    resource.parent.mkdir()
    resource.write_text('plain resource\n')
    entry=source.skills_service.snapshot_for(force_reload=True).resolve('sample')
    assert entry is not None
    selection={'stable_id':entry.stable_id,'content_sha256':entry.content_sha256,'resources':['references/sample.txt']}
    return source,target,other,selection,path


def control(host,operation,reference):
    home=host.home_paths
    owner=OwnerIdentity(home.owner_provider,home.owner_kind,home.owner_id.split('/')[-1])
    actor='ou_source' if owner.owner_kind=='user' else 'ou_receiver'
    scope=GatewayControlScope(actor,'feishu',owner.owner_id if owner.owner_kind=='group' else 'oc_private',resolved_owner=owner)
    return execute_transfer_control(host,scope,[operation,reference])


def prepare(case,*,clone=False):
    source,target,_,selection,_=case
    return skill_transfer.prepare_transfer(source,asdict(OwnerIdentity.provider_group('feishu','oc_target')),[selection],clone=clone)


def test_double_user_confirmation_and_exact_scope_import_resources(transfer_case):
    source,target,other,selection,path=transfer_case
    offer=prepare(transfer_case)
    reference=offer['reference']
    assert 'data' not in offer['manifest']['skills'][0]['files']['SKILL.md']
    assert not control(target,'accept',reference).ok
    assert not control(other,'show',reference).ok
    assert control(source,'authorize',reference).ok
    assert not control(other,'accept',reference).ok
    accepted=control(target,'accept',reference)
    assert accepted.ok,accepted.message
    assert control(target,'accept',reference).details==accepted.details
    name=accepted.details['imported'][0]
    store=SkillLearningStore.for_home(target.home_paths)
    assert store.skill_path(name).is_file()
    assert (store.skill_path(name).parent/'references/sample.txt').read_text()=='plain resource\n'
    assert target.skills_service.snapshot_for(force_reload=True).resolve(name) is not None
    assert source.home_paths.owner_home_dir not in store.skill_path(name).parents
    assert json.loads(SkillTransferTool(source).execute({'action':'status','reference':reference}).output)['status']=='authorized'
    assert not SkillTransferTool(source).execute({'action':'accept','reference':reference}).ok


def test_clone_once_per_receiver_owner_and_concurrent_replay(transfer_case):
    source,target,_,_,_=transfer_case
    offer=prepare(transfer_case,clone=True)
    assert control(source,'authorize',offer['reference']).ok
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:control(target,'accept',offer['reference']),range(2)))
    assert all(result.ok for result in results)
    assert results[0].details==results[1].details
    second=prepare(transfer_case,clone=True)
    assert control(source,'authorize',second['reference']).ok
    denied=control(target,'accept',second['reference'])
    assert denied.error_code=='SKILL_TRANSFER_CLONE_ALREADY_USED'
    registry=SkillLearningStore.for_home(target.home_paths).load_registry()
    assert sum(row.get('role')=='import' and row.get('status')=='committed' and row.get('clone') for row in registry.import_operations.values())==1


def test_failed_final_commit_rolls_back_and_retry_does_not_consume_clone(transfer_case,monkeypatch):
    source,target,_,_,_=transfer_case
    offer=prepare(transfer_case,clone=True)
    assert control(source,'authorize',offer['reference']).ok
    original=SkillLearningStore.save_registry
    armed=[True]
    def fail_final(store,registry):
        if store.skills_root==target.home_paths.owner_home_dir/'skills' and armed[0] and any(row.get('role')=='import' and row.get('status')=='committed' for row in registry.import_operations.values()):
            armed[0]=False
            raise OSError('synthetic write failure')
        return original(store,registry)
    monkeypatch.setattr(SkillLearningStore,'save_registry',fail_final)
    denied=control(target,'accept',offer['reference'])
    assert not denied.ok
    store=SkillLearningStore.for_home(target.home_paths)
    assert not store.load_registry().skills
    assert not list(store.learned_root.glob('*/SKILL.md'))
    assert control(target,'accept',offer['reference']).ok


@pytest.mark.parametrize('attack',['tamper','wrong_hash','symlink','traversal','secret','package'])
def test_source_manifest_rejects_tamper_and_unsafe_files(transfer_case,attack):
    source,target,_,selection,path=transfer_case
    selection=dict(selection)
    if attack=='wrong_hash':
        selection['content_sha256']='0'*64
    elif attack=='symlink':
        alias=path.parent/'references'/'link'
        alias.symlink_to(path)
        selection['resources']=['references/link']
    elif attack=='traversal':
        selection['resources']=['../SKILL.md']
    elif attack=='secret':
        resource=path.parent/'references'/'secret.txt'
        resource.write_text('api_key = "sk-'+('A'*48)+'"')
        selection['resources']=['references/secret.txt']
    elif attack=='package':
        original=source.current_skill_snapshot()
        source._current_skill_snapshot=SimpleNamespace(resolve=lambda ref:SimpleNamespace(**{**original.resolve('sample').__dict__,'source':'package:private'}))
    if attack=='tamper':
        offer=prepare(transfer_case)
        ref=offer['reference'][:-1]+('0' if offer['reference'][-1]!='0' else '1')
        assert not control(source,'authorize',ref).ok
    else:
        with pytest.raises(SkillTransferError):
            skill_transfer.prepare_transfer(source,asdict(OwnerIdentity.provider_group('feishu','oc_target')),[selection])


def test_quota_preflight_and_same_operation_after_capacity_repair(transfer_case):
    source,target,_,_,path=transfer_case
    (path.parent/'references/sample.txt').write_bytes(b'a'*700_000)
    offer=prepare(transfer_case,clone=True)
    assert control(source,'authorize',offer['reference']).ok
    quota=target.home_paths.owner_home_dir/'quota.json'
    payload=json.loads(quota.read_text())
    quota.write_text(json.dumps({**payload,'max_disk_mb':1}))
    result=control(target,'accept',offer['reference'])
    assert result.error_code=='OWNER_DISK_QUOTA_EXCEEDED'
    store=SkillLearningStore.for_home(target.home_paths)
    assert not store.load_registry().skills
    assert not list(store.learned_root.glob('*/SKILL.md'))
    quota.write_text(json.dumps({**payload,'max_disk_mb':5}))
    assert control(target,'accept',offer['reference']).ok


def test_memory_clone_uses_explicit_reviewed_candidates_not_chat_or_permissions(transfer_case):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation
    source,target,_,selection,_=transfer_case
    row=source.memory_candidates.observe(CandidateObservation(candidate_type='long_term_fact',content='明确可分享的工作方法事实',
        subject_key='shared-fact',scope={'scope_type':'global','scope_key':'global'},origin='user_explicit',
        evidence_refs=({'kind':'user_confirmation','ref_id':'synthetic'},),observation_id='source-user-confirmed'))
    if row.status!='pending_review':
        row=source.memory_candidates.transition(row.candidate_id,'pending_review',reviewer='source-human')
    source.memory_candidates.transition(row.candidate_id,'approved',reviewer='source-human')
    offer=skill_transfer.prepare_transfer(source,asdict(OwnerIdentity.provider_group('feishu','oc_target')),[selection],clone=True,
        memory_candidates=[row.candidate_id])
    assert control(source,'authorize',offer['reference']).ok
    result=control(target,'accept',offer['reference'])
    assert result.ok,result.message
    candidates=target.memory_candidates.list()
    selected=[item for item in candidates if item.content==row.content]
    assert len(selected)==1 and selected[0].status=='pending_review' and selected[0].promotion_mode=='manual_required'
    assert selected[0].source_message_refs==[] and selected[0].promotion_target=='long_term'
    assert result.details['memory_candidates']==1
    assert control(target,'accept',offer['reference']).ok
    assert len(target.memory_candidates.list())==len(candidates)


def test_uncommitted_batch_hidden_then_same_id_recovers_after_process_crash(transfer_case,monkeypatch):
    source,target,_,_,_=transfer_case
    offer=prepare(transfer_case,clone=True)
    assert control(source,'authorize',offer['reference']).ok
    original=SkillLearningStore.save_registry
    observations=[]
    armed=[True]
    def crash_final(store,registry):
        if store.skills_root==target.home_paths.owner_home_dir/'skills' and armed[0] and any(row.get('role')=='import' and row.get('status')=='committed' for row in registry.import_operations.values()):
            armed[0]=False
            observations.append([entry.name for entry in target.skills_service.snapshot_for(force_reload=True).entries])
            raise SystemExit('simulated-process-death')
        return original(store,registry)
    monkeypatch.setattr(SkillLearningStore,'save_registry',crash_final)
    with pytest.raises(SystemExit):
        control(target,'accept',offer['reference'])
    imported_name=offer['manifest']['skills'][0]['name']
    assert imported_name not in observations[0]
    assert target.skills_service.snapshot_for(force_reload=True).resolve(imported_name) is None
    result=control(target,'accept',offer['reference'])
    assert result.ok,result.message
    assert target.skills_service.snapshot_for(force_reload=True).resolve(imported_name) is not None


def test_repeat_same_source_version_deduplicates_without_overwrite(transfer_case):
    source,target,_,_,_=transfer_case
    one=prepare(transfer_case)
    assert control(source,'authorize',one['reference']).ok
    first=control(target,'accept',one['reference'])
    assert first.ok
    two=prepare(transfer_case)
    assert control(source,'authorize',two['reference']).ok
    second=control(target,'accept',two['reference'])
    assert second.ok and second.details['imported']==[] and second.details['duplicates']==first.details['imported']
    store=SkillLearningStore.for_home(target.home_paths)
    assert len(store.load_registry().skills)==1


def test_revoked_source_and_disabled_switch_do_not_import(transfer_case,monkeypatch):
    source,target,_,_,_=transfer_case
    offer=prepare(transfer_case)
    assert control(source,'authorize',offer['reference']).ok
    assert control(source,'revoke',offer['reference']).ok
    assert not control(target,'accept',offer['reference']).ok
    monkeypatch.setattr(skill_transfer,'read_fresh_capability_switch',lambda *args:False)
    assert control(source,'authorize',offer['reference']).error_code=='SKILL_TRANSFER_DISABLED'
    assert not SkillLearningStore.for_home(target.home_paths).load_registry().skills


def test_original_im_skills_parser_and_gateway_dispatch_are_usable_without_cli(transfer_case,tmp_path):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    source,target,_,_,_=transfer_case
    offer=prepare(transfer_case)
    paths=gateway_paths_from_root(tmp_path/'gateway')
    source_scope=GatewayControlScope('ou_source','feishu','oc_private',resolved_owner=OwnerIdentity.provider_user('feishu','ou_source'))
    target_scope=GatewayControlScope('ou_receiver','feishu','oc_target',resolved_owner=OwnerIdentity.provider_group('feishu','oc_target'))
    command=parse_conversation_control('/skills transfer authorize '+offer['reference'])
    assert command.valid and command.operation=='transfer'
    authorized=execute_gateway_conversation_control(source,paths,command,source_scope)
    assert authorized.ok,authorized.message
    accepted=execute_gateway_conversation_control(target,paths,parse_conversation_control('/skills transfer accept '+offer['reference']),target_scope)
    assert accepted.ok,accepted.message


@pytest.mark.parametrize('state',['unknown','departed'])
def test_current_group_actor_proof_required_for_accept(transfer_case,monkeypatch,state):
    from agent_py_agent.agent.conversation.host_actor import HostActorError
    source,target,_,_,_=transfer_case
    offer=prepare(transfer_case)
    assert control(source,'authorize',offer['reference']).ok
    def members(*args):
        if state=='unknown':
            raise HostActorError('SCHEDULER_MEMBERSHIP_UNAVAILABLE')
        return set()
    monkeypatch.setattr('agent_py_agent.agent.scheduler.creator_access.current_group_members',members)
    result=control(target,'accept',offer['reference'])
    assert result.error_code==('SKILL_TRANSFER_MEMBERSHIP_UNAVAILABLE' if state=='unknown' else 'SKILL_TRANSFER_NOT_AUTHORIZED')
    assert not SkillLearningStore.for_home(target.home_paths).load_registry().skills


def test_user_modified_partial_crash_copy_is_preserved_instead_of_deleted(transfer_case,monkeypatch):
    source,target,_,_,_=transfer_case
    offer=prepare(transfer_case,clone=True)
    assert control(source,'authorize',offer['reference']).ok
    original=SkillLearningStore.save_registry
    armed=[True]
    def crash(store,registry):
        if store.skills_root==target.home_paths.owner_home_dir/'skills' and armed[0] and any(row.get('role')=='import' and row.get('status')=='committed' for row in registry.import_operations.values()):
            armed[0]=False
            raise SystemExit('process-death')
        return original(store,registry)
    monkeypatch.setattr(SkillLearningStore,'save_registry',crash)
    with pytest.raises(SystemExit):
        control(target,'accept',offer['reference'])
    name=offer['manifest']['skills'][0]['name']
    path=target.home_paths.owner_home_dir/'skills/learned'/name/'references/sample.txt'
    path.write_text('user-owned replacement')
    result=control(target,'accept',offer['reference'])
    assert result.error_code=='SKILL_TRANSFER_OPERATION_CONFLICT'
    assert path.read_text()=='user-owned replacement'
    assert not SkillLearningStore.for_home(target.home_paths).load_registry().skills


def test_received_resources_use_existing_file_and_sandbox_permissions(transfer_case):
    import shlex

    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
    source,target,_,selection,path=transfer_case
    script=path.parent/'scripts'/'sum.py'
    script.parent.mkdir()
    script.write_text('print(sum([2, 3, 5]))\n')
    novel=path.parent/'references'/'sample.unregistered-format'
    novel.write_bytes(b'open-format-resource\x00\x01')
    path.write_text(path.read_text()+'\n运行[求和脚本](scripts/sum.py)，仅处理本地合成数字。\n')
    entry=source.skills_service.snapshot_for(force_reload=True).resolve('sample')
    selection.update(content_sha256=entry.content_sha256,resources=['scripts/sum.py','references/sample.unregistered-format'])
    offer=prepare(transfer_case)
    assert control(source,'authorize',offer['reference']).ok
    accepted=control(target,'accept',offer['reference'])
    assert accepted.ok,accepted.message
    installed=SkillLearningStore.for_home(target.home_paths).skill_path(accepted.details['imported'][0])
    read=execute_registry_test_call(target.tools,'read_file',{'path':str(installed.parent/'scripts/sum.py')},register_with=target)
    assert read.ok,read.output
    assert 'sum' in read.output
    command='/usr/bin/python3 '+shlex.quote(str(installed.parent/'scripts/sum.py'))
    run=execute_registry_test_call(target.tools,'run_command',{'command':command,'timeout':5},register_with=target,call_id='installed-script')
    assert run.ok,run.output
    assert '10' in run.output
    assert (installed.parent/'references/sample.unregistered-format').read_bytes()==novel.read_bytes()
    cross=execute_registry_test_call(target.tools,'read_file',{'path':str(script)},register_with=target,call_id='source-cross-owner')
    assert not cross.ok
    protected=execute_registry_test_call(target.tools,'write_file',{'path':str(target.home_paths.owner_permissions_json),'content':'{}'},register_with=target,call_id='protected-policy')
    assert not protected.ok
