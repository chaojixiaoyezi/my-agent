# LLM: 原ProcessSession是唯一进程事实；C仅no-follow读规范记录，W原P5/短T内才取原事务并发布原Wake，联测MP发现/停止/去重与Count0。
# 模块用途: 将后台命令已登记的完成通知接入原多进程来源运输，不造Agent、定时器或第二会话账。
from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path

from ..common.nofollow_fs import NoFollowPathError, read_bytes_beneath
from ..common.strict_json import load_strict_json
from ..conversation.process_events import publish_process_completion
from ..tooling.process_session_records import (
    PROCESS_TERMINAL_STATUSES,
    validate_process_record,
    validate_session_id,
)
from ..tooling.process_session_store import ProcessSessionStore, process_session_store_root
from ..user_space.owner_resolver import _owner_home_dir, _owner_home_result

_MAX_RECORD_BYTES = 1_048_576


# LLM: ref仅原session文件名，真实父目录永远从已核owner定位；完整raw摘要与原版本revision共同限制旧运输。
# 函数用途: 拒绝任意目录、缺线程或畸形进程引用。
def validate_process_completion_ref(ref):
    session_id = validate_session_id(ref['source_id'])
    if (ref['source_ref'] != session_id + '.json' or not ref['source_exists']
            or not ref['thread_id'] or not ref['store_ref'] or ref['input_revision'] != ref['source_sha256']):
        raise ValueError('background_process_completion_ref_invalid')
    store = Path(ref['store_ref'])
    if (not store.parts or store.is_absolute() or store.as_posix() != ref['store_ref']
            or any(part in {'.', '..'} for part in store.parts)):
        raise ValueError('background_process_completion_ref_invalid')


# LLM: C不调用Store load/list/redo；原规范地址由owner纯函数固定，no-follow完整读不接受模型路径或索引代权威。
# 函数用途: 只读一个已存在的原会话，缺失保持缺失，坏记录原样拒绝。
def _read_record(home, session_id):
    root = process_session_store_root(home, home)
    relative = root.relative_to(home.parent).parts + (validate_session_id(session_id) + '.json',)
    raw = read_bytes_beneath(home.parent, relative, max_bytes=_MAX_RECORD_BYTES, require_dir_fd=True)
    if raw is None:
        return None, None
    record = validate_process_record(load_strict_json(raw))
    if record['session_id'] != session_id:
        raise ValueError('background_process_filename_identity_mismatch')
    return record, hashlib.sha256(raw).hexdigest()


# LLM: 原access/target必须完整匹配可信owner/store；完成地址仅收件事实，不授工具或跨owner执行权限。
# 函数用途: 核对这条欠通知记录确实属于本次原会话存储。
def _matches_target(home_root, owner, home, store_root, record):
    target = record.get('completion_target') or {}
    access = record['access_scope']
    return (bool(target) and record.get('activation_scope') is None
        and access.get('owner_home') == str(home)
        and access.get('owner_id') == _owner_home_result(Path(home_root), owner, home).owner_id
        and target['thread_id'] == access['conversation_id']
        and target['store_root'] == str(store_root))


# LLM: 仅原C配置解析得到的Store用于资格核验；ref冻结整record raw，不将终态目录变化当其它session的变化。
# 函数用途: 为一个已终态且欠通知的原会话生成规范运输引用。
def freeze_process_completion(home_root, owner, store, session_id):
    from .multiprocess_background_request import source_owner

    source_owner(asdict(owner))
    home = _owner_home_dir(Path(home_root), owner)
    record, digest = _read_record(home, session_id)
    if (record is None or record['status'] not in PROCESS_TERMINAL_STATUSES
            or record.get('completion_notice_id') or not record.get('completion_target')):
        return None
    if not _matches_target(home_root, owner, home, store.storage.root, record):
        raise ValueError('background_process_completion_owner_target_mismatch')
    return {'schema_version':'gateway_background_source.v1','kind':'process_completion','owner':asdict(owner),
        'source_ref':session_id+'.json','source_exists':True,'source_sha256':digest,'input_revision':digest,
        'store_ref':Path(store.storage.root).relative_to(home).as_posix(),
        'source_id':session_id,'thread_id':record['completion_target']['thread_id']}


# LLM: 只读规范owner配置与原Store元数据；坏session显式记录原诊断并继续其它条目，不构造业务Agent/恢复redo。
# 函数用途: 将当前分页中的原终态记录转换为可运输的完成通知候选。
def discover_process_completions(control, owner, paths):
    if not paths:
        return
    from .owner_conversation_store import owner_conversation_store_from_config
    from .owner_resources import resolve_owner_resource_configuration

    home, config, _ = resolve_owner_resource_configuration(control, owner)
    if not getattr(config, "background_process_notifications", True):
        return
    store = owner_conversation_store_from_config(config, home, initialize=False)
    for path in paths:
        try:
            ref = freeze_process_completion(control.home_root, owner, store, path.stem)
            if ref is not None:
                yield ref
        except (OSError, ValueError, TypeError) as error:
            from .logging import _report_gateway_side_effect_error
            _report_gateway_side_effect_error("process_completion_discovery", owner.owner_id, error)


# LLM: 每次重读原完整记录且核target/ref，来源换代只拒旧运输，不消费新会话或修复损坏。
# 函数用途: 返回仍与冻结引用完全一致的原会话记录。
def read_process_completion_source(home_root, ref):
    from .multiprocess_background_request import BackgroundSourceChanged, source_owner

    owner = source_owner(ref['owner'])
    home = _owner_home_dir(Path(home_root), owner)
    record, digest = _read_record(home, ref['source_id'])
    if record is None or digest != ref['source_sha256']:
        raise BackgroundSourceChanged()
    if (not _matches_target(home_root, owner, home, home / ref['store_ref'], record)
            or record['completion_target']['thread_id'] != ref['thread_id']):
        raise ValueError('background_process_completion_owner_target_mismatch')
    return record


# LLM: receipt本地写失败保原pending运输；actual异常不吞。调用者必须先持原background短T；本事务重核整record后才发原dedupe Wake并写原notice，停止/换代/未知不复活。
# 函数用途: 消费准确owner会话的一次完成通知，保留发布后回执失败的原重投合同。
def consume_process_completion(agent, ref):
    from ..backends.bounded_call import BoundedCallExitUnconfirmedError
    from ..common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from ..common.tool_write_context import ToolWriteContextUnavailable
    from ..tooling.process_session_commit import ProcessSessionCommitPendingError
    from .multiprocess_background_request import BackgroundSourceChanged, read_source

    if not getattr(agent.config, 'background_process_notifications', True):
        return 'source_changed'
    home = agent.home_paths.owner_home_dir
    if Path(agent.conversation_store.storage.root).relative_to(home).as_posix() != ref['store_ref']:
        raise ValueError('background_source_store_mismatch')
    root = process_session_store_root(home, home)
    authority = ProcessSessionStore(root)
    if authority.root != root:
        raise ValueError('background_process_store_anchor_changed')
    with authority.transaction() as transaction:
        try:
            expected = read_source(agent.home_paths.root, ref)
        except BackgroundSourceChanged:
            return 'source_changed'
        current = transaction.load(ref['source_id'])
        if (current != expected or current['status'] not in PROCESS_TERMINAL_STATUSES
                or current.get('completion_notice_id')):
            return 'source_changed'
        try:
            notice_id = publish_process_completion(agent.conversation_store, current)
            transaction.write({**current, 'completion_notice_id':notice_id})
        except (NoFollowPathError, PermissionError, NotADirectoryError):
            raise
        except ProcessSessionCommitPendingError as error:
            # 原redo已发表不等于底层actual/硬错可被洗成普通重试；只认原typed cause，不读文本。
            if isinstance(error.__cause__, (RequestExecutionOutcomeUncertainError, BoundedCallExitUnconfirmedError,
                    RequestExecutionContextUnavailable, ToolWriteContextUnavailable,
                    NoFollowPathError, PermissionError, NotADirectoryError)):
                raise error.__cause__ from None
            return 'pending'
        except OSError:
            # 普通本地发表/回执I/O失败仍欠通知，原pending/dedupe补齐而不重跑命令。
            return 'pending'
    return 'handled'
