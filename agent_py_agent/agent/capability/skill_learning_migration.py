# LLM: S1原committed提案只提供来源，不另建维护库；仅精确目标/hash/来源有效且未被用户修改时纳入原S3 registry，保持原路径和stable_id。
# 模块用途: 将确认后的lesson纳入统一维护权威，不凭lesson目录名接管用户文件。
from __future__ import annotations

import time
import uuid
from dataclasses import replace
from pathlib import Path

from ..common.nofollow_fs import NoFollowPathError, read_text_beneath
from .skill_learning_store import LearnedSkill, skill_family_fingerprint, utc_now
from .skill_snapshot import skill_content_sha256


# LLM: S1 失败隔离按提案粒度处理（缺候选来源或读正文失败跳过当前条目，不阻塞后续有效提案）；
#   只初始化 incarnation_id / activation_id / task_family_fingerprint 与 trial 状态，绝不伪造 established_at。
# 函数用途: 纳管仍与原确认回执一致的 S1 正文，来源不明或手改内容跳过。
def adopt_committed_lessons(store, proposal_service):
    from ..memory_store.candidates import CandidateService
    from .skill_proposals import SkillProposalError, _verify_source

    adopted = []
    proposals = proposal_service.list(status="committed")
    candidates = CandidateService(proposal_service.candidates_path)
    with store.state_lock():
        registry = store._prepare_clean_registry_locked(store.load_registry())
        skills = dict(registry.skills)
        for proposal in proposals:
            name = proposal.target.skill_name
            if name in skills or name in registry.blocked_names:
                continue
            path = store.skills_root / name / 'SKILL.md'
            if path.is_symlink() or not path.is_file() or Path(proposal.receipt.get('skill_path', '')) != path:
                continue
            try:
                text = read_text_beneath(store.skills_root, (name, 'SKILL.md'))
            except (OSError, ValueError, UnicodeError, NoFollowPathError):
                continue
            if text is None:
                continue
            sha = skill_content_sha256(text)
            if sha != proposal.draft.sha256 or sha != proposal.receipt.get('skill_sha256'):
                continue
            family = skill_family_fingerprint(text)
            if family in registry.blocked_task_families:
                continue
            try:
                _verify_source(candidates, proposal.source)
            except (SkillProposalError, OSError, ValueError):
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
            store.save_version(name, 1, text)
            skills[name] = record
            adopted.append(name)
        if adopted:
            store.save_registry(replace(registry, skills=skills))
    return adopted
