# LLM: S1原committed提案只提供来源，不另建维护库；仅精确目标/hash/来源有效且未被用户修改时纳入原S3 registry，保持原路径和stable_id。
# 模块用途: 将确认后的lesson纳入统一维护权威，不凭lesson目录名接管用户文件。
from __future__ import annotations

import time
import uuid
from dataclasses import replace
from pathlib import Path

from ..common.nofollow_fs import NoFollowPathError, list_names_beneath, read_text_beneath
from .skill_learning_store import (
    LearnedSkill,
    owner_layout_exempt,
    skill_family_fingerprint,
    utc_now,
)
from .skill_snapshot import skill_content_sha256


# LLM: S1 失败隔离按提案粒度处理（缺候选来源或读正文失败跳过当前条目，不阻塞后续有效提案）；
#   只初始化 incarnation_id / activation_id / task_family_fingerprint 与 trial 状态，绝不伪造 established_at。
# 函数用途: 纳管仍与原确认回执一致的 S1 正文，来源不明或手改内容跳过。
def adopt_committed_lessons(store, proposal_service):
    from ..memory_store.candidates import CandidateService
    from .skill_proposals import (
        CODE_SOURCE_MISSING,
        SkillProposalError,
        _verify_source,
        render_skill_markdown,
    )

    adopted = []
    proposals = proposal_service.list(status="committed")
    candidates = CandidateService(proposal_service.candidates_path)
    with store.state_lock():
        registry = store._prepare_clean_registry_locked(store.load_registry())
        skills = dict(registry.skills)
        for proposal in proposals:
            name = proposal.target.skill_name
            current = skills.get(name)
            if name in registry.blocked_names:
                continue
            if current is not None and (
                not owner_layout_exempt(current)
                or current.maintenance.get('ownership') == 'user_modified'
                or current.maintenance.get('source_proposal_id') != proposal.proposal_id
                or tuple(current.source_run_ids) != tuple(proposal.source.run_ids)
            ):
                continue
            # 人工确认的业务资产不自动收归机器；旧机器回执须有原始来源与安全裁决。
            auto = (
                proposal.receipt.get('confirmed_by') == 'auto'
                and proposal.receipt.get('guard_trust_level') == 'agent_generated'
                and proposal.receipt.get('guard_verdict') == 'safe'
                and bool(proposal.source.run_ids)
                and bool(proposal.source.task_ids)
                and bool(proposal.source.candidate_id)
                and bool(proposal.source.content_hash)
            )
            if current is not None and not auto:
                continue
            path = store.skills_root / name / 'SKILL.md'
            if path.is_symlink() or not path.is_file() or Path(proposal.receipt.get('skill_path', '')) != path:
                continue
            try:
                if set(list_names_beneath(store.skills_root, (name,))) != {'SKILL.md'}:
                    continue
                text = read_text_beneath(store.skills_root, (name, 'SKILL.md'))
            except (OSError, ValueError, UnicodeError, NoFollowPathError):
                continue
            if text is None:
                continue
            sha = skill_content_sha256(text)
            if (sha != proposal.draft.sha256 or sha != proposal.receipt.get('skill_sha256')
                    or sha != skill_content_sha256(render_skill_markdown(name, proposal.draft))
                    or (current is not None and sha != current.sha256)):
                continue
            family = skill_family_fingerprint(text)
            if family in registry.blocked_task_families:
                continue
            source_verification = 'live_candidate'
            try:
                _verify_source(candidates, proposal.source)
            except SkillProposalError as exc:
                # 已登记的旧安装回执是原来源核验后的耐久事实；候选已按保留期清理不撤销它。
                # 仅允许明确缺失，仍存在但已修改/拒绝/脱敏的来源继续拒绝纳管。
                if not (current is not None and auto and exc.code == CODE_SOURCE_MISSING):
                    continue
                source_verification = 'committed_receipt_source_missing'
            except (OSError, ValueError):
                continue
            now = utc_now()
            now_ts = time.time()
            record = LearnedSkill(
                name,
                1,
                sha,
                now,
                now,
                tuple(proposal.source.run_ids),
                layout='owner',
                maintenance={
                    'state': 'trial',
                    'quality': 'trial',
                    'established_at': 0.0,
                    'baseline_at': now_ts,
                    'source_proposal_id': proposal.proposal_id,
                    'incarnation_id': uuid.uuid4().hex,
                    'activation_id': uuid.uuid4().hex,
                    'task_family_fingerprint': family,
                },
            )
            if current is not None:
                record = replace(current, maintenance={**current.maintenance, **{
                    key: value for key, value in record.maintenance.items()
                    if key not in current.maintenance
                }})
            if auto:
                record = replace(record, maintenance={
                    **record.maintenance,
                    'ownership': 'machine',
                    'lifecycle_adoption': {
                        'schema': 'committed_proposal.v1',
                        'proposal_id': proposal.proposal_id,
                        'candidate_id': proposal.source.candidate_id,
                        'source_content_hash': proposal.source.content_hash,
                        'original_sha256': sha,
                        'source_verification': source_verification,
                        'adopted_at': now_ts,
                    },
                })
            # 已有版本全文若不同则保留现场，不覆盖用户或历史记录。
            try:
                previous = store.read_version(name, record.revision)
                if previous is not None and skill_content_sha256(previous) != sha:
                    continue
                if previous is None:
                    store.save_version(name, record.revision, text)
            except (OSError, ValueError, UnicodeError, NoFollowPathError):
                # 单条历史版本损坏只能隔离本条，不能让其它已核验采纳事实全部丢失。
                continue
            skills[name] = record
            adopted.append(name)
        if adopted:
            store.save_registry(replace(registry, skills=skills, maintenance_cursor={}))
    return adopted
