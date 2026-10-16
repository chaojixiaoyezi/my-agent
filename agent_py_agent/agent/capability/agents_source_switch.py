# LLM: 原切源锁内核授权/CAS/意图，退役与URL校验在C短门外，最终配置另核同原CAS/字节/权限，不新增账。
# 模块用途: 拆平配置提交前后校验和资源退役，保持唯一来源仓库的小步事务。
import time
from dataclasses import replace
from pathlib import Path

from ._agents_contract_fields import AgentsContractError
from .agents_source_contract import validate_source_snapshot
from .agents_source_files import read_source_bytes, write_control_json
from .agents_source_processes import retire_source_resources


# LLM: 原来源锁/进程边界内退役；管理最终短门复读原CAS/本地身份或门外已核URL缓存字节，不重做DNS/join。
# 函数用途: 提交一次已经绑定原授权的来源变更，未知停止或并发改动不落配置。
def set_source_locked(repo, kind, locator, context):
    current, old_record = repo._configuration_record()
    validate_source_snapshot(current, repo.owner, context.target_cas)
    _require_old_source_content(repo, (current, old_record), context)
    proposed = replace(current, kind=kind, locator=locator, revision=current.revision + 1)
    proposed.to_dict()
    if kind == "owner_default" and locator != str(repo.default_path):
        raise AgentsContractError("AGENTS_SOURCE_OWNER_MISMATCH", "locator")
    repo.require_permission(proposed)
    identity = _proposed_identity(repo, proposed)
    record = dict(schema_version=1, owner=repo.owner, kind=kind, value=locator,
                  revision=proposed.revision, updated_by=context.actor,
                  updated_at=float(time.time()), identity=identity)
    retire_source_resources(repo.owner_home, proposed.configuration_cas)
    # LLM: 退役/网络复核已在门外完成；最后只核完整原CAS/本地身份或已核缓存原字节，以及当前策略，再写唯一配置。
    # 函数用途: 在原C仍有效的短窗口提交配置，不在门内重做DNS或stop/join。
    def commit():
        validate_source_snapshot(repo.configuration(), repo.owner, context.target_cas)
        if repo._file_commit is not None and current.kind == "url" and context.content_sha256:
            from .agents_source_cache import require_saved_source_digest
            require_saved_source_digest(repo, current, context.content_sha256)
        else:
            _require_old_source_content(repo, (current, old_record), context)
        repo.require_permission(proposed, validate_network=False)
        if _proposed_identity(repo, proposed) != identity:
            raise AgentsContractError("AGENTS_SOURCE_IDENTITY_CHANGED", "locator")
        write_control_json(repo.owner_home, repo.config_path.name, record)
    repo.commit_file(commit)
    return proposed


# LLM: 保留原可选SHA语义；仍由同一repository按本地实际正文/URL last-good核对，不拉新网络内容。
# 函数用途: 退役前后复核授权明确绑定的旧正文。
def _require_old_source_content(repo, state, context):
    if context.content_sha256:
        repo._require_current_sha(*state, context.content_sha256)


# LLM: URL不读本地文件；本地两次都用原nofollow/同FD读取和严格解码，不能仅比定位字符串。
# 函数用途: 取得候选来源当前安全身份，供退役前后比较。
def _proposed_identity(repo, proposed):
    if proposed.kind == "url":
        return ""
    raw, identity = read_source_bytes(Path(proposed.locator))
    repo._decode(raw)
    return identity
