# LLM: S1原committed提案只提供来源，不另建维护库；仅精确目标/hash/来源有效且未被用户修改时纳入原S3 registry，保持原路径和stable_id。
# 模块用途: 将确认后的lesson纳入统一维护权威，不凭lesson目录名接管用户文件。
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..common.nofollow_fs import read_text_beneath
from .skill_learning_store import LearnedSkill, utc_now
from .skill_snapshot import skill_content_sha256


# LLM: 只读取原提案服务已验证的committed对象，不移动文件；版本保存和registry在同一owner配额事务。
# 函数用途: 纳管一份仍与原确认回执一致的S1正文，来源不明或手改内容跳过。
def adopt_committed_lessons(store, proposal_service):
    adopted = []
    proposals = proposal_service.list(status="committed")
    with store.state_lock():
        registry = store.load_registry()
        skills = dict(registry.skills)
        for proposal in proposals:
            name = proposal.target.skill_name
            if name in skills or name in registry.blocked_names:
                continue
            path = store.skills_root/name/'SKILL.md'
            if path.is_symlink() or not path.is_file() or Path(proposal.receipt.get('skill_path','')) != path:
                continue
            text = read_text_beneath(store.skills_root,(name,'SKILL.md'))
            if text is None:
                continue
            sha = skill_content_sha256(text)
            if sha != proposal.draft.sha256 or sha != proposal.receipt.get('skill_sha256'):
                continue
            from ..memory_store.candidates import CandidateService
            from .skill_proposals import _verify_source
            _verify_source(CandidateService(proposal_service.candidates_path),proposal.source)
            now = utc_now()
            record = LearnedSkill(name,1,sha,now,now,tuple(proposal.source.run_ids),layout='owner',
                maintenance={'state':'trial','source_proposal_id':proposal.proposal_id})
            store.save_version(name,1,text)
            skills[name] = record
            adopted.append(name)
        if adopted:
            store.save_registry(replace(registry,skills=skills))
    return adopted
