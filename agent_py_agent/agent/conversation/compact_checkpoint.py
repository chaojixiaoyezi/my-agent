# LLM: v3候选在原owner账本追加，thread head/CAS仍是唯一提交权威；scope/base/精确覆盖与摘要一起封印，
# v1/v2仅通过原版本合同读取。局部摘要不能因在同一提交链就扩大适用范围，孤立候选不可用于隐藏来源。
# 模块用途: 保存双来源恢复点并由原线程CAS确认；读取按同次行地址逐行校验，旧摘要无需全驻留。

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.json_io import append_private_jsonl_records_unlocked, locked_json_path
from ..common.nofollow_fs import (
    read_bytes_beneath,
    write_bytes_atomic_beneath,
    write_text_atomic_beneath,
)
from .compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from .compact_tool_identity import compact_tool_refs
from .message_replay import message_rows_iterator
from .models import ConversationThread, MessageLogEntry

_COLD_PAYLOAD_FIELDS = ('operation_evidence', 'provider_compaction')
_MAX_COLD_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_ACTIVE_COMPACT_CHECKPOINTS = 32
_ACTIVE_CHECKPOINT_HIGH_WATER = 64
_ACTIVE_CHECKPOINT_BYTE_BUDGET = 2 * 1024 * 1024

if TYPE_CHECKING:
    from ..agent_core.runtime.context_compactor import RuntimeCompactPolicy
    from ..core import SimpleAgent


# LLM: 同一候选的scope/base、原消息和计量必须对齐；引用是同链读取投影，不授予任务权限。
# 类用途: 汇总摘要范围、可重放来源及近期尾部，交给原检查点写入，不要求正文tuple常驻。
@dataclass(frozen=True)
class CompactCheckpointRequest:
    thread: ConversationThread
    summary: str
    operation_evidence: dict[str, object]
    compact_rows: Sequence[MessageLogEntry]
    retained_tail: Sequence[MessageLogEntry]
    source_end_byte_offset: int
    projected_tokens_before: int
    projected_tokens_after: int
    policy: RuntimeCompactPolicy
    forced: bool
    scope: CompactScope = THREAD_COMPACT_SCOPE
    summary_base_checkpoint_id: str | None = None
    source_tool_refs: tuple[dict[str, str], ...] = ()
    retained_tool_refs: tuple[dict[str, str], ...] = ()
    # LLM: 媒体策略事实只在本次覆盖范围含媒体块时写入 checkpoint；refs 为完整 sha256，供审计与用户按内容地址重新附上。
    #   archived 计按归档引用处理的图块，summarized 计看图小请求总结过的图块；B 部分成功时两者同时非零（reason=vision_digest_partial）；
#   reason 另记 auto 下没走 B 的结构化原因。
    media_policy: str = ""
    media_fact_source: str = ""
    media_blocks_archived: int = 0
    media_blocks_summarized: int = 0
    media_policy_reason: str = ""
    media_refs: tuple[str, ...] = ()
    # 服务端压缩记录（conversation/compact_remote.provider_compaction_record）；None 表示普通文字摘要。
    provider_compaction: dict[str, object] | None = None


# LLM: 工具来源/保留区均以原四元refs为权威，call_id仅作展示；与scope/base共同写入原owner账本。
# 类用途: 保存一次活动工具压缩候选及准确来源，避免跨轮同名调用被误替代。
@dataclass(frozen=True)
class LiveToolCompactCheckpointRequest:
    thread: ConversationThread
    summary: str
    source_tool_call_ids: tuple[str, ...]
    retained_tool_call_ids: tuple[str, ...]
    projected_tokens_before: int
    projected_tokens_after: int
    policy: RuntimeCompactPolicy
    request_id: str
    attempt_id: str
    source_tool_refs: tuple[dict[str, str], ...]
    retained_tool_refs: tuple[dict[str, str], ...] = ()
    forced: bool = False
    scope: CompactScope = THREAD_COMPACT_SCOPE
    summary_base_checkpoint_id: str | None = None
    provider_compaction: dict[str, object] | None = None


# LLM: v3身份绑定提交前驱、摘要基础、scope、精确覆盖及创建时间；新冷引用与provider范围同样封印。
# 旧inline v3字节身份不变；水合对象仍用冷引用而非重复正文计算身份，热行不能借inline字段覆盖冷证据。
# 重试在新时间创建的是另一候选，原CAS仍只允许一个获胜；旧v1/v2不能使用新身份算法写入。
# 函数用途: 为同一提交候选生成内容身份，防止摘要相同但适用范围不同的候选混同。
def scoped_compact_checkpoint_id(row: dict[str, object]) -> str:
    keys = ("schema", "thread_id", "generation", "previous_checkpoint_id", "summary_base_checkpoint_id",
            "scope", "source_kind", "source_message_ids", "source_tool_refs", "summary_sha256",
            "operation_evidence", "source_end_byte_offset", "retained_tail_message_ids", "retained_tool_call_ids",
            "created_at", "retained_tool_refs")
    values = {key: row.get(key) for key in keys}
    for name in _COLD_PAYLOAD_FIELDS:
        if f'{name}_ref' in row:
            values[name] = None
            values[f'{name}_ref'] = row[f'{name}_ref']
    if 'provider_compaction_ref' in row:
        values['provider_compaction_info'] = row.get('provider_compaction_info')
    if 'carried_view' in row:
        values['carried_view'] = row['carried_view']
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"compact-v3-{row['generation']}-{digest}"


# LLM: 固定owner根由原home_paths提供；线程必须是单个路径组件，冷引用不接受任意相对路径。
# 函数用途: 获取本owner检查点目录并拒绝路径型线程身份。
def _compact_root(agent, thread_id):
    raw_root = str(getattr(getattr(agent, 'home_paths', None), 'owner_compact_dir', '') or '').strip()
    if not raw_root:
        raise OSError('owner compact directory is unavailable')
    if not isinstance(thread_id, str) or not thread_id or thread_id in {'.', '..'} or Path(thread_id).name != thread_id:
        raise OSError('conversation compact thread identity is invalid')
    return Path(raw_root)


# LLM: 私有JSON采用indent=2/sort_keys/尾LF，与原原子JSON写者同字节格式；目录和叶均走严格no-follow。
# 已存在内容地址必须逐字相同，不覆盖坏文件；文件落盘先于候选发布，失败最多留下不可见冷孤儿。
# 函数用途: 将完整载荷耐久保存为0600内容寻址对象，返回封印所需的摘要、长度和固定文件名。
def _write_cold_payload(root, thread_id, payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    raw = text.encode('utf-8')
    if len(raw) > _MAX_COLD_PAYLOAD_BYTES:
        raise ValueError('conversation compact cold payload exceeds byte budget')
    digest = hashlib.sha256(raw).hexdigest()
    parts = ('evidence', thread_id, f'{digest}.json')
    previous = read_bytes_beneath(root, parts, max_bytes=len(raw))
    if previous is not None and previous != raw:
        raise OSError('conversation compact cold payload digest mismatches')
    if previous is None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_text_atomic_beneath(root, parts, text)
    return {'sha256': digest, 'byte_length': len(raw), 'ref': parts[-1]}


# LLM: 长度和摘要来自封印引用；完整父链及叶均不跟随链接，不以缺失、坏JSON或空载荷降级成功。
# 函数用途: 按需恢复本owner的一份冷证据或供应商记录，验证固定路径、字节长度和SHA256。
def _read_cold_payload(root, thread_id, ref):
    if type(ref) is not dict or set(ref) != {'sha256', 'byte_length', 'ref'}:
        raise OSError('conversation compact cold payload reference is invalid')
    digest, size = ref['sha256'], ref['byte_length']
    if (type(digest) is not str or len(digest) != 64 or any(ch not in '0123456789abcdef' for ch in digest)
            or type(size) is not int or not 0 < size <= _MAX_COLD_PAYLOAD_BYTES
            or ref['ref'] != f'{digest}.json'):
        raise OSError('conversation compact cold payload reference is invalid')
    raw = read_bytes_beneath(root, ('evidence', thread_id, ref['ref']), max_bytes=size)
    if raw is None or len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise OSError('conversation compact cold payload digest or length mismatches')
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise OSError('conversation compact cold payload is unreadable') from exc
    if type(payload) is not dict:
        raise OSError('conversation compact cold payload must be an object')
    return payload


# LLM: 默认显式完整链水合两类载荷；summary reader只在选中后调用，保留旧inline记录原值。
# 冷引用存在时始终从冷对象恢复，忽略热行里可能被伪造的同名inline字段。
# 函数用途: 在不修改原行的情况下恢复实际需要的完整操作证据及供应商密文。
def hydrate_compact_checkpoint(agent, row):
    result = dict(row)
    root = _compact_root(agent, row['thread_id'])
    for name in _COLD_PAYLOAD_FIELDS:
        ref = row.get(f'{name}_ref')
        if ref is not None:
            result[name] = _read_cold_payload(root, row['thread_id'], ref)
    return result


# LLM: 摘要基础是适用视图而不是链head；显式基础也必须是该准备代次的同一适用摘要，不能暗中跨任务继承。
# 函数用途: 校验原提交链并固定候选将要继承的摘要引用，不写持久状态。
def _summary_base(agent, request) -> str:
    from .compact_summary_view import resolve_compact_summary_view

    view = resolve_compact_summary_view(agent, request.thread, request.scope)
    requested = request.summary_base_checkpoint_id
    if requested is not None and requested != view.checkpoint_id:
        raise ValueError("compact summary base changed or is outside the requested scope")
    return view.checkpoint_id


# LLM: 所有来源共用v3头和原用量事实；source字段由各来源提供，元数据不授予运行权限。
# 函数用途: 固定检查点的同链前驱、摘要基础、范围和预算，避免两种压缩产生不同权威。
def _checkpoint_payload(agent, request) -> dict[str, object]:
    thread = request.thread
    summary = str(request.summary or "").strip()
    if not summary:
        raise ValueError("compact checkpoint requires a summary")
    backend = getattr(agent, "backend", None)
    return {
        "schema": "conversation_compact_checkpoint.v3", "event": "conversation_compact_checkpoint",
        "status": "validated_candidate", "commit_authority": "conversation_thread.compact_checkpoint_id",
        "previous_checkpoint_id": thread.compact_checkpoint_id, "thread_id": thread.thread_id,
        "previous_generation": thread.compact_generation, "generation": thread.compact_generation + 1,
        "summary_base_checkpoint_id": _summary_base(agent, request), "scope": request.scope.to_dict(),
        "source_message_ids": [], "source_tool_refs": [],
        "source_start_byte_offset": thread.compacted_through_byte_offset,
        "source_end_byte_offset": thread.compacted_through_byte_offset,
        "source_messages": 0, "source_messages_total": thread.compact_source_messages,
        "source_tool_pairs": 0, "source_tool_pairs_total": thread.compact_source_tool_pairs,
        "projected_tokens_before": max(0, int(request.projected_tokens_before)),
        "projected_tokens_after": max(0, int(request.projected_tokens_after)),
        "context_window_tokens": request.policy.context_window_tokens,
        "trigger_percent": request.policy.trigger_percent, "trigger_tokens": request.policy.trigger_tokens,
        "recovery_target_tokens": request.policy.recovery_target_tokens, "forced": bool(request.forced),
        "summary": summary, "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "backend": str(getattr(backend, "name", "") or ""),
        "model": str(getattr(backend, "model_name", "") or ""), "created_at": time.time(),
        # 服务端压缩检查点：summary 是占位文本，真正的内容在这条记录的压缩项里（协议 + 端点决定谁能读）。
        **({"provider_compaction": dict(request.provider_compaction)}
           if isinstance(getattr(request, "provider_compaction", None), dict) else {}),
    }


# LLM: 唯一追加路径先落私有冷载荷，封印引用并检查最终行预算后才追加候选；head仍由原CAS发布。
# 函数用途: 将带冷证据引用的候选写入原owner检查点账本，拒绝不可读超长行且不更新线程。
def _append_checkpoint(agent, row: dict[str, object]) -> str:
    from .compact_checkpoint_scan import _CHECKPOINT_ROW_MAX_BYTES

    root = _compact_root(agent, row['thread_id'])
    for name in _COLD_PAYLOAD_FIELDS:
        if name in row:
            payload = row.pop(name)
            if type(payload) is not dict:
                raise ValueError('conversation compact cold payload must be an object')
            row[f'{name}_ref'] = _write_cold_payload(root, row['thread_id'], payload)
            if name == 'provider_compaction':
                row['provider_compaction_info'] = {key: payload.get(key) for key in ('protocol', 'endpoint')}
    row["checkpoint_id"] = scoped_compact_checkpoint_id(row)
    if len((json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')) >= min(_CHECKPOINT_ROW_MAX_BYTES, _ACTIVE_CHECKPOINT_BYTE_BUDGET):
        raise ValueError('conversation compact checkpoint record exceeds byte budget')
    # 私有追加：0600/0700，存量宽权限 checkpoint 账本下次写入即收紧；内容逐字节不变。
    _save_checkpoint_copy(root, row)
    path = root / 'conversations' / f"{row['thread_id']}.jsonl"
    with locked_json_path(path):
        append_private_jsonl_records_unlocked(path, [row], sort_keys=True)
        _maintain_active_checkpoints(root, row['thread_id'], path)
    return row["checkpoint_id"]


# LLM: 同一候选记录双覆盖；短路校验显式关闭来源，双refs与摘要封印后只经一次CAS发布，不改live-tool写入。
# 函数用途: 从同一只读消息序列保存精确覆盖和保留IDs，持久schema及原CAS不变。
def write_compact_checkpoint(agent: SimpleAgent, request: CompactCheckpointRequest) -> str:
    compact_rows = request.compact_rows
    with message_rows_iterator(compact_rows) as rows:
        foreign_source = any(row.thread_id != request.thread.thread_id for row in rows)
    if not compact_rows or foreign_source:
        raise ValueError("conversation compact checkpoint requires source messages from its thread")
    source_ids = [item.message_id for item in compact_rows]
    if any(not isinstance(item, str) or not item.strip() for item in source_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("conversation compact source messages need distinct identities")
    refs = compact_tool_refs(list(request.source_tool_refs))
    retained_refs = compact_tool_refs(list(request.retained_tool_refs))
    from .compact_tool_identity import compact_tool_ref_key

    if {compact_tool_ref_key(ref) for ref in refs} & {compact_tool_ref_key(ref) for ref in retained_refs}:
        raise ValueError("compact source and retained tools overlap")
    row = _checkpoint_payload(agent, request)
    row.update({
        "source_kind": "transcript_and_tool_archive" if refs else "transcript", "source_start_message_id": compact_rows[0].message_id,
        "source_end_message_id": compact_rows[-1].message_id,
        "source_end_byte_offset": max(0, int(request.source_end_byte_offset)),
        "source_message_ids": source_ids,
        "source_messages": len(compact_rows),
        "source_tool_refs": list(refs), "source_tool_call_ids": [ref["call_id"] for ref in refs],
        "source_tool_pairs": len(refs), "source_tool_pairs_total": request.thread.compact_source_tool_pairs + len(refs),
        "retained_tool_refs": list(retained_refs), "retained_tool_call_ids": [ref["call_id"] for ref in retained_refs],
        "retained_tool_pairs": len(retained_refs),
        "source_messages_total": request.thread.compact_source_messages + len(compact_rows),
        "retained_tail_start_message_id": request.retained_tail[0].message_id if request.retained_tail else "",
        "retained_tail_end_message_id": request.retained_tail[-1].message_id if request.retained_tail else "",
        "retained_tail_message_ids": [item.message_id for item in request.retained_tail],
        "retained_tail_messages": len(request.retained_tail),
        "operation_evidence": json.loads(json.dumps(request.operation_evidence, ensure_ascii=False)),
    })
    if request.media_policy and (request.media_blocks_archived > 0 or request.media_blocks_summarized > 0):
        row.update({
            "media_policy": request.media_policy, "media_fact_source": request.media_fact_source,
            "media_blocks_archived": int(request.media_blocks_archived),
            "media_blocks_summarized": int(request.media_blocks_summarized), "media_refs": list(request.media_refs),
            **({"media_policy_reason": request.media_policy_reason} if request.media_policy_reason else {}),
        })
    row['carried_view'] = _prepare_carried_view(agent, request, row)
    return _append_checkpoint(agent, row)


# LLM: 运行中摘要覆盖原调用四元身份，不能把摘要请求的request/attempt当作被压调用来源；缺失时拒绝写候选。
# 函数用途: 保存精确工具覆盖和同一摘要基础，仍不改变原工具账或全线程游标。
def write_live_tool_compact_checkpoint(agent: SimpleAgent, request: LiveToolCompactCheckpointRequest) -> str:
    refs = compact_tool_refs(list(request.source_tool_refs))
    retained_refs = compact_tool_refs(list(request.retained_tool_refs))
    source_ids = tuple(ref["call_id"] for ref in refs)
    if not source_ids:
        raise ValueError("live tool compact checkpoint requires source tool pairs")
    if source_ids != request.source_tool_call_ids or tuple(ref["call_id"] for ref in retained_refs) != request.retained_tool_call_ids:
        raise ValueError("compact call projection does not match exact references")
    row = _checkpoint_payload(agent, request)
    base_evidence = request.thread.compact_operation_evidence
    if request.scope.kind != "thread":
        from .compact_summary_view import resolve_compact_summary_view

        base_evidence = resolve_compact_summary_view(agent, request.thread, request.scope).operation_evidence
    row.update({
        "source_kind": "live_tool_ir", "source_start_message_id": "", "source_end_message_id": "",
        "source_tool_call_ids": list(source_ids), "source_tool_refs": list(refs),
        "source_tool_pairs": len(refs), "source_tool_pairs_total": request.thread.compact_source_tool_pairs + len(refs),
        "retained_tool_call_ids": list(request.retained_tool_call_ids),
        "retained_tool_pairs": len(retained_refs), "retained_tool_refs": list(retained_refs),
        "request_id": str(request.request_id or ""), "attempt_id": str(request.attempt_id or ""),
        "operation_evidence": json.loads(json.dumps(base_evidence, ensure_ascii=False)),
    })
    row['carried_view'] = _prepare_carried_view(agent, request, row)
    return _append_checkpoint(agent, row)


# LLM: 完整chain接口保留旧顺序及行形状，校验共用唯一逆向reader；完整返回者仍承担全部正文内存。
# 函数用途: 为需要检查全部快照的调用方返回从旧到新的已提交链，不把orphan算入结果。
def committed_compact_checkpoint_chain(agent: SimpleAgent, thread: ConversationThread) -> tuple[dict[str, object], ...]:
    with closing(iter_committed_compact_checkpoints(agent, thread)) as chain:
        return tuple(reversed(tuple(chain)))


# LLM: 从原thread head逆序逐行校验后yield；消费者必须完整耗尽后才能采用，失败/提前退出须close以释放描述符。
# v3内容封印及旧版本合同不变，原writer/CAS唯一；hydrate=False只供摘要元数据选择，完整接口默认恢复冷载荷。
# 函数用途: 为摘要视图提供最新到最旧的只读检查点，完整链不成立时拒绝恢复。
def iter_committed_compact_checkpoints(agent: SimpleAgent, thread: ConversationThread, *, hydrate: bool = True):
    checkpoint_id = str(thread.compact_checkpoint_id or "").strip()
    generation = max(0, int(thread.compact_generation or 0))
    if not checkpoint_id and generation == 0:
        return
    if not checkpoint_id or generation <= 0:
        raise OSError("conversation compact pointer is incomplete")
    home = getattr(agent, "home_paths", None)
    raw_root = str(getattr(home, "owner_compact_dir", "") or "").strip()
    if not raw_root:
        raise OSError("owner compact directory is unavailable")
    from .compact_checkpoint_scan import checkpoint_row_lookup

    path = Path(raw_root) / "conversations" / f"{thread.thread_id}.jsonl"
    with checkpoint_row_lookup(path) as lookup:
        visited: set[str] = set()
        current_id = checkpoint_id
        expected_generation = generation
        while current_id:
            if current_id in visited:
                raise OSError("conversation compact checkpoint chain contains a cycle")
            visited.add(current_id)
            row = lookup(current_id)
            if row is None:
                row = _cold_checkpoint(Path(raw_root), thread.thread_id, current_id)
            if row is None:
                raise OSError("conversation compact checkpoint chain is incomplete")
            if str(row.get("thread_id") or "").strip() != thread.thread_id:
                raise OSError("conversation compact checkpoint thread identity mismatches")
            try:
                row_generation = int(row.get("generation") or 0)
            except (TypeError, ValueError) as exc:
                raise OSError("conversation compact checkpoint generation is invalid") from exc
            if row_generation != expected_generation:
                raise OSError("conversation compact checkpoint generation chain mismatches")
            is_v3 = row.get("schema") == "conversation_compact_checkpoint.v3"
            if is_v3 != current_id.startswith("compact-v3-"):
                raise OSError("conversation compact checkpoint version identity mismatches")
            if not is_v3 and any(key in row for key in ("scope", "summary_base_checkpoint_id", "source_tool_refs", "source_message_ids")):
                raise OSError("legacy conversation compact checkpoint contains new scope fields")
            if is_v3 and scoped_compact_checkpoint_id(row) != current_id:
                raise OSError("conversation compact checkpoint content identity mismatches")
            current_id = str(row.get("previous_checkpoint_id") or "").strip()
            expected_generation -= 1
            if hydrate:
                yield hydrate_compact_checkpoint(agent, row)
            elif 'provider_compaction_ref' in row:
                yield {**row, 'provider_compaction': row.get('provider_compaction_info')}
            else:
                yield row
        if expected_generation != 0:
            raise OSError("conversation compact checkpoint chain ended early")


# LLM: 覆盖来自实际适用摘要的语义基础链，不能把同提交链其它任务的来源一起隐藏。
# 函数用途: 返回全线程摘要真正覆盖的工具引用；局部宿主应传入同次已应用的摘要视图。
def committed_live_tool_compact_source_refs(agent: SimpleAgent, thread: ConversationThread) -> tuple[dict[str, object], ...]:
    from .compact_summary_view import resolve_compact_summary_view

    return resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE).source_tool_refs


# LLM: 记录副本是不可变冷存储，不是提交权威；只有原线程head或其封印目录可赋予可见性。
# 函数用途: 将候选元数据按原内容身份耐久保存，允许热账本轮转后准确读取。
def _save_checkpoint_copy(root, row):
    checkpoint_id, thread_id = row['checkpoint_id'], row['thread_id']
    name = hashlib.sha256(checkpoint_id.encode('utf-8')).hexdigest() + '.checkpoint.json'
    raw = (json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    parts = ('archive', thread_id, name)
    previous = read_bytes_beneath(root, parts, max_bytes=len(raw))
    if previous is not None and previous != raw:
        raise OSError('conversation compact immutable checkpoint copy mismatches')
    if previous is None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_bytes_atomic_beneath(root, parts, raw)


# LLM: 固定文件名只由expected ID哈希构成；读取后仍核原v3封印，不以冷副本存在代替原head。
# 函数用途: 按明确身份读取归档检查点，不枚举归档目录或历史代次。
def _cold_checkpoint(root, thread_id, checkpoint_id):
    from .compact_checkpoint_scan import _CHECKPOINT_ROW_MAX_BYTES, _parse_row

    name = hashlib.sha256(checkpoint_id.encode('utf-8')).hexdigest() + '.checkpoint.json'
    raw = read_bytes_beneath(root, ('archive', thread_id, name), max_bytes=_CHECKPOINT_ROW_MAX_BYTES)
    if raw is None:
        return None
    row = _parse_row(raw)
    if row is None or row.get('checkpoint_id') != checkpoint_id or row.get('thread_id') != thread_id:
        raise OSError('conversation compact archived checkpoint identity mismatches')
    if row.get('schema') == 'conversation_compact_checkpoint.v3' and scoped_compact_checkpoint_id(row) != checkpoint_id:
        raise OSError('conversation compact checkpoint content identity mismatches')
    return row


# LLM: 持canonical账本锁；冷副本与冷段先落盘，再原子替换热文件，最后旁车。旁车可重建且不确认提交。
# 函数用途: 同时约束热账本条数与严格小于2MiB的字节数，保存地址、文件身份和归档水位。
def _maintain_active_checkpoints(root, thread_id, path):
    from .compact_checkpoint_scan import _checkpoint_byte_rows, _parse_row

    with path.open('rb', buffering=0) as handle:
        through = handle.seek(0, 2)
        handle.seek(0)
        rows = [(raw, _parse_row(raw)) for _, raw in _checkpoint_byte_rows(handle, through)]
    rows = [(raw, row) for raw, row in rows if row is not None]
    archived_floor = 0
    if len(rows) > _ACTIVE_CHECKPOINT_HIGH_WATER or sum(len(raw) for raw, _ in rows) >= _ACTIVE_CHECKPOINT_BYTE_BUDGET:
        keep, retained_bytes = 0, 0
        for raw, _row in reversed(rows):
            if keep >= MAX_ACTIVE_COMPACT_CHECKPOINTS or retained_bytes + len(raw) >= _ACTIVE_CHECKPOINT_BYTE_BUDGET:
                break
            keep += 1
            retained_bytes += len(raw)
        if not keep:
            raise ValueError('conversation compact checkpoint exceeds hot byte budget')
        old, rows = rows[:-keep], rows[-keep:]
        for _, row in old:
            if row.get('thread_id') != thread_id:
                raise OSError('conversation compact archive thread identity mismatches')
            _save_checkpoint_copy(root, row)
        if old:
            raw = b''.join(raw for raw, _ in old)
            first, last = int(old[0][1]['generation']), int(old[-1][1]['generation'])
            name = f'gen-{first}-{last}-{hashlib.sha256(raw).hexdigest()[:16]}.jsonl'
            write_bytes_atomic_beneath(root, ('archive', thread_id, name), raw)
            archived_floor = last
        write_bytes_atomic_beneath(root, ('conversations', path.name), b''.join(raw for raw, _ in rows))
    stamp = path.stat()
    offsets, cursor = {}, 0
    for raw, row in rows:
        offsets[row['checkpoint_id']] = {
            'offset': cursor, 'length': len(raw), 'generation': row['generation'],
            'sha256': hashlib.sha256(raw).hexdigest(),
        }
        cursor += len(raw)
    meta = {
        'schema': 'conversation_compact_meta.v1', 'thread_id': thread_id,
        'dev': stamp.st_dev, 'ino': stamp.st_ino, 'size': stamp.st_size, 'offsets': offsets,
        'latest_generation': max((int(row['generation']) for _, row in rows), default=0),
        'archived_up_to_generation': archived_floor, 'commit_authority': 'conversation_thread.compact_checkpoint_id',
    }
    write_text_atomic_beneath(root, ('conversations', path.with_suffix('.meta.json').name),
                              json.dumps(meta, ensure_ascii=False, sort_keys=True) + '\n')


# LLM: 有界热区包括未采用scope，仍核对其摘要；域回调不是通用scanner的默认schema，更换回调要重验缓存。
# 函数用途: 保留未采用已提交摘要损坏也拒绝恢复的原合同。
def _validate_hot_checkpoint_row(row):
    from .compact_summary_view import _validate_summary

    if 'summary' in row and 'summary_sha256' in row:
        _validate_summary(row)


# LLM: 此接口只寻址原线程确切head，旁车不可信时改读有界热文件；旧v3由原完整链路径解释。
# 函数用途: 常数上界恢复当前已提交的单条检查点，验证身份与代次而不走历史链。
def load_latest_committed_compact_checkpoint(agent, thread):
    from .compact_checkpoint_scan import checkpoint_row_lookup

    checkpoint_id = str(thread.compact_checkpoint_id or '')
    generation = int(thread.compact_generation or 0)
    if not checkpoint_id and generation == 0:
        return None
    if not checkpoint_id or generation <= 0:
        raise OSError('conversation compact pointer is incomplete')
    root = _compact_root(agent, thread.thread_id)
    path = root / 'conversations' / f'{thread.thread_id}.jsonl'
    with checkpoint_row_lookup(path, validator=_validate_hot_checkpoint_row) as lookup:
        row = lookup(checkpoint_id)
    if row is None:
        row = _cold_checkpoint(root, thread.thread_id, checkpoint_id)
    if row is None:
        raise OSError('conversation compact checkpoint chain is incomplete')
    if row.get('thread_id') != thread.thread_id or row.get('generation') != generation:
        raise OSError('conversation compact checkpoint generation or thread mismatches')
    is_v3 = row.get('schema') == 'conversation_compact_checkpoint.v3'
    if is_v3 != checkpoint_id.startswith('compact-v3-'):
        raise OSError('conversation compact checkpoint version identity mismatches')
    if is_v3 and scoped_compact_checkpoint_id(row) != checkpoint_id:
        raise OSError('conversation compact checkpoint content identity mismatches')
    receipt = (thread.metadata or {}).get('compact_commit_boundary')
    expected = {'checkpoint_id': checkpoint_id, 'generation': generation,
                'boundary_message_id': thread.compacted_through_message_id,
                'summary_sha256': hashlib.sha256(thread.summary.encode('utf-8')).hexdigest()}
    if receipt is not None and receipt != expected:
        raise OSError('conversation compact committed boundary mismatches')
    return row


# LLM: scope键完整保留所有身份；时间键固定宽度，浮点时间正数的IEEE大端序与数值次序一致。
# 函数用途: 为不可变目录生成准确作用域键和允许时钟回退的时间上界键。
def _scope_key(scope):
    return json.dumps(scope.to_dict(), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


# LLM: generation作为同时间后缀，边界查询用全f包含同一时间的所有已提交代次。
# 函数用途: 将正时间及代次编码成固定64位有序索引键。
def _timeline_key(created_at, generation=None):
    import struct

    prefix = struct.pack('>d', float(created_at)).hex()
    return prefix + (f'{generation:016x}' if generation is not None else 'f' * 16) + 'f' * 32


# LLM: 普通摘要对所有后端可读；密文只按原protocol/endpoint身份归类，不以模型名扩大可读范围。
# 函数用途: 给摘要base链的兼容回退目录生成固定族键。
def _readability_key(record):
    return '*' if record is None else json.dumps(
        {key: record.get(key) for key in ('protocol', 'endpoint')}, sort_keys=True, separators=(',', ':'),
    )


# LLM: 新锚点只继承实际summary_base的覆盖，提交前驱只更新可选scope目录；所有根都由新候选ID封印。
# 函数用途: 把累计覆盖和scope/后端回退目录改成固定深度不可变根，热行仅保留有界近期引用。
def _prepare_carried_view(agent, request, row):
    from .compact_summary_view import resolve_compact_summary_view
    from .store_io import ImmutableJsonIndex

    root, thread_id = _compact_root(agent, row['thread_id']), row['thread_id']
    index = ImmutableJsonIndex(root, ('evidence', thread_id, 'indexes'))
    timeline = ImmutableJsonIndex(root, ('evidence', thread_id, 'timeline'), ordered=True)
    view = resolve_compact_summary_view(agent, request.thread, request.scope)
    base = view.carried_metadata
    previous = load_latest_committed_compact_checkpoint(agent, request.thread)
    previous_meta = previous.get('carried_view', {}) if previous is not None else {}
    scopes, times = previous_meta.get('scope_heads'), previous_meta.get('thread_timeline')
    if previous is not None:
        if not previous_meta:
            # 旧格式仅在首次新写入时完整验证、建目录；后续代次不再重复历史链。
            for old in reversed(tuple(iter_committed_compact_checkpoints(agent, request.thread, hydrate=False))):
                _save_checkpoint_copy(root, old)
                descriptor = _checkpoint_descriptor(old)
                from .compact_summary_view import _checkpoint_scope

                old_scope = _checkpoint_scope(old)
                scopes = index.put(scopes, _scope_key(old_scope), descriptor)
                if old_scope == THREAD_COMPACT_SCOPE:
                    times = timeline.put(times, _timeline_key(old['created_at'], old['generation']), descriptor)
        else:
            descriptor = _checkpoint_descriptor(previous)
            scopes = index.put(scopes, _scope_key(CompactScope.from_dict(previous['scope'])), descriptor)
            if CompactScope.from_dict(previous['scope']) == THREAD_COMPACT_SCOPE:
                times = timeline.put(times, _timeline_key(previous['created_at'], previous['generation']), descriptor)
    messages, tools = base.get('messages'), base.get('tools')
    message_count, tool_count = int(base.get('message_count', 0)), int(base.get('tool_count', 0))
    if not base and view.checkpoint_id:
        for identity in view.source_message_ids:
            messages = index.put(messages, identity, True)
            message_count += 1
        for ref in view.source_tool_refs:
            tools = index.put(tools, json.dumps(ref, sort_keys=True), {'ref': ref, 'order': tool_count})
            tool_count += 1
    for identity in row['source_message_ids']:
        if index.get(messages, identity) is None:
            messages = index.put(messages, identity, True)
            message_count += 1
    for ref in row['source_tool_refs']:
        key = json.dumps(ref, sort_keys=True)
        if index.get(tools, key) is None:
            tools = index.put(tools, key, {'ref': ref, 'order': tool_count})
            tool_count += 1
    recent_messages = list(dict.fromkeys([*(base['recent_messages'] if base else tuple(view.source_message_ids)),
                                        *row['source_message_ids']]))[-256:]
    recent_tools = {json.dumps(ref, sort_keys=True): ref for ref in (
        *(base['recent_tools'] if base else view.source_tool_refs), *row['source_tool_refs'],
    )}
    variants = base.get('readable_bases')
    lineage = base.get('checkpoint_lineage')
    if view.checkpoint_id:
        lineage = index.put(lineage, view.checkpoint_id, True)
        old = _cold_checkpoint(root, thread_id, view.checkpoint_id)
        if old is not None:
            variants = index.put(variants, _readability_key(old.get('provider_compaction_info', old.get('provider_compaction'))),
                                 _checkpoint_descriptor(old))
    return {
        'schema': 'conversation_carried_view.v1', 'base_checkpoint_id': row['summary_base_checkpoint_id'],
        'scope': row['scope'], 'messages': messages, 'tools': tools,
        'message_count': message_count, 'tool_count': tool_count,
        'recent_messages': recent_messages, 'recent_tools': list(recent_tools.values())[-128:],
        'legacy_message_end_ids': list(view.legacy_message_end_ids),
        'scope_heads': scopes, 'thread_timeline': times, 'readable_bases': variants,
        'checkpoint_lineage': lineage,
    }


# LLM: 目录值不复制摘要或证据，原ID和generation将由读取者核对；候选未提交不会进入获胜者的目录。
# 函数用途: 提供不可变目录最小寻址事实。
def _checkpoint_descriptor(row):
    return {'checkpoint_id': row['checkpoint_id'], 'generation': row['generation'], 'created_at': row['created_at']}


__all__ = [
    "CompactCheckpointRequest",
    "LiveToolCompactCheckpointRequest",
    "scoped_compact_checkpoint_id",
    "committed_compact_checkpoint_chain",
    "committed_live_tool_compact_source_refs",
    "write_live_tool_compact_checkpoint",
    "write_compact_checkpoint",
]
