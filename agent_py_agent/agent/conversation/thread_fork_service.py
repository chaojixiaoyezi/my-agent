# LLM: 主会话分叉只读源记录，写新消息、同owner的Compact链与人格快照后最后发布新线程；不复制运行授权或任务绑定。
# 模块用途: 创建独立主会话；跨实例快照仅导入不可信文本，不继承原人格、Compact、工具授权或身份。
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from ..common.json_io import locked_json_path, write_private_text_file_atomic
from ..common.nofollow_fs import read_bytes_beneath, unlink_file_beneath
from .compact_checkpoint import committed_compact_checkpoint_chain, scoped_compact_checkpoint_id
from .compact_scope import THREAD_COMPACT_SCOPE
from .compact_summary_view import resolve_compact_summary_view
from .compact_tool_identity import compact_tool_ref_key
from .models import ConversationThread, MessageLogEntry, new_id
from .store_io import now
from .store_messages import fork_message_prefix
from .store_threads import create_thread_atomic
from .workspace_context import validate_workspace_directory

if TYPE_CHECKING:
    from .store import ConversationStore


# LLM: 格式错误提供稳定代码，调用方可映射控制回执，不通过自然语言判定执行。
# 类用途: 表示分叉参数、源数据或安全边界不成立。
class ConversationForkError(ValueError):
    # LLM: 错误代码是结构字段，附带文字只用于展示，不包含消息正文。
    # 函数用途: 构造可被CLI或宿主识别的分叉错误。
    def __init__(self, code: str, detail: str = "") -> None:
        self.error_code = code
        super().__init__(f"{code}: {detail}" if detail else code)


# LLM: 优先结构化native信封；普通正文不升级协议，旧assistant JSON只有被真实tool角色对应时才识别，用户示例永不解释为调用。
# 函数用途: 产出一条消息携带的原生工具消息，保留原对象不修改。
def _tool_payloads(row: MessageLogEntry, legacy_results: frozenset[str] = frozenset()) -> list[dict]:
    metadata = row.metadata
    native = metadata.get("canonical_native_messages")
    if isinstance(native, dict) and native.get("schema") == "conversation_native_messages.v1":
        native = native.get("messages")
    if isinstance(native, (list, tuple)):
        return [item for item in native if isinstance(item, dict)]
    content = metadata.get("content", row.content)
    if row.role in {"tool", "assistant"} and isinstance(content, str) and content.lstrip().startswith(("{", "[")):
        try:
            candidate = json.loads(content)
            if row.role == "tool" or (isinstance(candidate, dict) and any(
                    kind == "call" and call_id in legacy_results for kind, call_id in _tool_events(candidate))):
                content = candidate
        except (ValueError, TypeError):
            pass
    payload = {**metadata, "role": row.role, "content": content}
    if isinstance(content, dict):
        payload = {**metadata, "role": row.role, **content}
    return [payload]


# LLM: 同时识别provider消息和canonical块，只返回call/result身份；任意新content块保持普通正文。
# 函数用途: 提取一条原生消息的工具调用和结果顺序。
def _tool_events(payload: dict) -> list[tuple[str, str]]:
    events = []
    calls = payload.get("tool_calls")
    if isinstance(calls, list):
        events.extend(("call", str(item.get("id") or "")) for item in calls if isinstance(item, dict))
    if payload.get("role") == "tool" and payload.get("type") not in {"tool_result", "function_call_output"}:
        events.append(("result", str(payload.get("tool_call_id") or payload.get("call_id") or "")))
    blocks = payload.get("content")
    blocks = blocks if isinstance(blocks, list) else [blocks] if isinstance(blocks, dict) else []
    for block in [payload, *blocks]:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"tool_use", "function_call", "tool_call"}:
            events.append(("call", str(block.get("call_id") or block.get("id") or "")))
        elif kind in {"tool_result", "function_call_output"}:
            events.append(("result", str(block.get("tool_use_id") or block.get("tool_call_id") or block.get("call_id") or "")))
    return events


# LLM: 在消息级边界回退，不编辑或截短单个native envelope；孤立结果、重复call与未闭合并行调用均不能回放。
# 函数用途: 找到指定前缀以内最后一个完整工具回合，返回源消息前缀与是否调整。
def _safe_prefix(rows: list[MessageLogEntry], cutoff: str | None) -> tuple[list[MessageLogEntry], bool]:
    requested = len(rows)
    if cutoff is not None:
        positions = [index for index, row in enumerate(rows) if row.message_id == cutoff]
        if not positions:
            raise ConversationForkError("FORK_MESSAGE_NOT_FOUND")
        requested = positions[0] + 1
    legacy_results = frozenset(call_id for row in rows if row.role == "tool"
        for payload in _tool_payloads(row) for kind, call_id in _tool_events(payload) if kind == "result")
    pending: set[str] = set()
    safe = 0
    for index, row in enumerate(rows[:requested]):
        valid = True
        for payload in _tool_payloads(row, legacy_results):
            for kind, call_id in _tool_events(payload):
                if not call_id or (kind == "call" and call_id in pending) or (kind == "result" and call_id not in pending):
                    valid = False
                    break
                if kind == "call":
                    pending.add(call_id)
                else:
                    pending.remove(call_id)
            if not valid:
                break
        if not valid:
            break
        if not pending:
            safe = index + 1
    return rows[:safe], safe != requested


# LLM: owner home只能来自显式参数或源线程的宿主字段；不回退真实Path.home，防止测试或旧线程误触其它owner。
# 函数用途: 确定可读人格/Compact的同owner家目录，拒绝冲突参数。
def _owner_home(source: ConversationThread, owner_home: Path | None) -> Path | None:
    recorded = Path(source.owner_home).expanduser().resolve() if source.owner_home else None
    requested = owner_home.expanduser().resolve() if owner_home is not None else None
    if recorded is not None and requested is not None and recorded != requested:
        raise ConversationForkError("FORK_OWNER_MISMATCH")
    return recorded or requested


# LLM: 只复制类型化偏好，不复制metadata或运行授权；新发表标记由宿主独立生成，仅用于失败回收归属，不授予执行权。
# 函数用途: 构造空闲新线程并分配本次独有发表标记，防止同ID碰撞被误认为本次创建物。
def _new_thread(source: ConversationThread, rows: list[MessageLogEntry], title: str | None,
                workspace_dir: str | None, home: Path | None, is_admin: bool) -> ConversationThread:
    root, cwd, roots = source.workspace_root, source.cwd, source.runtime_workspace_roots
    if workspace_dir is None:
        _validate_inherited_workspace(source, home)
    if workspace_dir is not None:
        if home is None:
            raise ConversationForkError("WORKSPACE_DIR_INVALID", "缺少用户家目录")
        try:
            root = str(validate_workspace_directory(workspace_dir, owner_home=home, is_admin=is_admin))
        except ValueError as exc:
            raise ConversationForkError("WORKSPACE_DIR_INVALID", str(exc)) from exc
        cwd, roots = root, (root,)
    return ConversationThread(
        thread_id=new_id("thread"), canonical_user_id=source.canonical_user_id,
        owner_id=source.owner_id, owner_home=source.owner_home or str(home or ""),
        title=title if title is not None else source.title, cwd=cwd, workspace_root=root, runtime_workspace_roots=roots,
        forked_from_thread_id=source.thread_id, forked_from_message_id=rows[-1].message_id if rows else "",
        fork_lineage_depth=source.fork_lineage_depth + 1, model_profile_id=source.model_profile_id,
        metadata={'fork_publication_id': new_id('fork-publish')},
        model_selection_revision=source.model_selection_revision, model_selection_source=source.model_selection_source,
        model_selection_last_explicit_revision=source.model_selection_last_explicit_revision,
        model_selection_selected_at=source.model_selection_selected_at, reasoning_effort=source.reasoning_effort,
        verbose_level=source.verbose_level, decision_settings=copy.deepcopy(source.decision_settings),
        created_at=now(None), updated_at=now(None),
    )


# LLM: 继承只复制已有绑定，不新增执行授权；此处重验存在性、控制面和规范路径，真实身份权限仍由每轮工具边界核对。
# 函数用途: 拒绝在源会话绑定后失效或被符号链接重定向的目录，保持合法管理员外部工作根继承兼容。
def _validate_inherited_workspace(source: ConversationThread, home: Path | None) -> None:
    from .workspace_context import existing_workspace_directory

    for value in dict.fromkeys((source.cwd, source.workspace_root, *source.runtime_workspace_roots)):
        if not value:
            continue
        try:
            path = (validate_workspace_directory(value, owner_home=home, is_admin=True) if home is not None
                else existing_workspace_directory(value))
            if Path(value).expanduser() != path:
                raise ValueError("已绑定路径被重定向")
        except ValueError as exc:
            raise ConversationForkError("WORKSPACE_DIR_INVALID", str(exc)) from exc


# LLM: 原始消息ID和字节游标属于旧文件，必须按新序列重新计算；序列化与后续写盘共用同一份字节。
# 函数用途: 生成新消息正文及旧ID到新ID/新字节结束位置的映射，不写文件。
def _message_copy(rows: list[MessageLogEntry], thread_id: str) -> tuple[str, dict[str, str], dict[str, int]]:
    clones = fork_message_prefix(rows, thread_id)
    id_map, offsets, text, offset = {}, {}, [], 0
    for original, clone in zip(rows, clones):
        line = json.dumps(clone.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        offset += len(line.encode("utf-8"))
        text.append(line)
        id_map[original.message_id], offsets[original.message_id] = clone.message_id, offset
    return "".join(text), id_map, offsets


# LLM: v3沿精确覆盖，旧v1沿原终点展开；只接受全线程且来源均在前缀内，不把局部或活动工具摘要跨任务继承。
# 函数用途: 判断检查点是否可以继承，返回该行在原消息中的精确覆盖。
def _checkpoint_sources(row: dict, rows: list[MessageLogEntry], id_map: dict[str, str]) -> list[str] | None:
    if row.get("schema") == "conversation_compact_checkpoint.v3":
        if row.get("scope") != THREAD_COMPACT_SCOPE.to_dict():
            return None
        ids = row.get("source_message_ids", [])
    elif row.get("schema") == "conversation_compact_checkpoint.v1":
        end = row.get("source_end_message_id")
        positions = [index for index, item in enumerate(rows) if item.message_id == end]
        ids = [item.message_id for item in rows[:positions[0] + 1]] if positions else []
    else:
        return None
    if not ids or any(message_id not in id_map for message_id in ids):
        return None
    return list(ids)


# LLM: 所有层级的消息引用沿核验共用键规则重映射，再封印新thread/游标/前驱；普通正文和工具四元身份不改。
# 函数用途: 将已验证检查点及嵌套消息引用重建为新线程候选，允许合法多级分叉。
def _remap_checkpoint(row: dict, ids: list[str], branch: ConversationThread, id_map: dict, offsets: dict,
                      checkpoint_map: dict) -> dict:
    mapped = copy.deepcopy(row)
    _remap_checkpoint_message_refs(mapped, id_map)
    previous = checkpoint_map.get(str(row.get("previous_checkpoint_id") or ""), "")
    base = row.get("summary_base_checkpoint_id", row.get("previous_checkpoint_id", ""))
    tail = [value for value in row.get("retained_tail_message_ids", []) if value in id_map]
    ordered_ids = list(id_map)
    first_index = ordered_ids.index(ids[0])
    start_offset = offsets[ordered_ids[first_index - 1]] if first_index else 0
    mapped.update({
        "schema": "conversation_compact_checkpoint.v3", "thread_id": branch.thread_id,
        "scope": THREAD_COMPACT_SCOPE.to_dict(), "previous_checkpoint_id": previous,
        "summary_base_checkpoint_id": checkpoint_map.get(str(base), ""),
        "source_message_ids": [id_map[value] for value in ids], "source_tool_refs": mapped.get("source_tool_refs", []),
        "source_start_message_id": id_map[ids[0]], "source_end_message_id": id_map[ids[-1]],
        "source_start_byte_offset": start_offset,
        "source_end_byte_offset": offsets[ids[-1]], "retained_tail_message_ids": [id_map[value] for value in tail],
        "retained_tail_start_message_id": id_map[tail[0]] if tail else "",
        "retained_tail_end_message_id": id_map[tail[-1]] if tail else "",
        "retained_tail_messages": len(tail), "retained_tool_refs": mapped.get("retained_tool_refs", []),
        "retained_tool_call_ids": row.get("retained_tool_call_ids", []),
    })
    mapped["checkpoint_id"] = scoped_compact_checkpoint_id(mapped)
    return mapped


# LLM: 本规则同时供前缀核验与重映射使用，避免新增可核验键却仍携带旧线程ID。
# 函数用途: 识别单个或复数结构化消息引用字段，不将普通文本当作身份。
def _is_checkpoint_message_reference(key):
    return key in {'message_id', 'message_ids'} or key.endswith(('_message_id', '_message_ids'))


# LLM: 只修改已深复制的候选，所有已核验旧引用按同一id_map转换一次，未知正文不变。
# 函数用途: 迭代重映射检查点各层消息引用，避免递归深度和连续分叉遗留旧ID。
def _remap_checkpoint_message_refs(mapped, id_map):
    pending = [mapped]
    while pending:
        value = pending.pop()
        if isinstance(value, (list, tuple)):
            pending.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        for key, child in value.items():
            if not _is_checkpoint_message_reference(key):
                pending.append(child)
                continue
            if isinstance(child, (list, tuple)):
                value[key] = type(child)(id_map.get(item, item) for item in child)
            elif isinstance(child, str) and child:
                value[key] = id_map.get(child, child)


# LLM: 原head链通过后仍核全部消息及裸ID/四元工具引用；四元身份缺证明即排除，不复制未来摘要或缺base的后继。
# 函数用途: 重建同owner压缩链和线程投影，避免跨运行的同名工具引用误继承。
def _compact_copy(source: ConversationThread, branch: ConversationThread, rows: list[MessageLogEntry],
                  home: Path | None, id_map: dict, offsets: dict) -> tuple[ConversationThread, str]:
    if not source.compact_checkpoint_id and not source.compact_generation:
        return branch, ""
    if home is None:
        raise ConversationForkError("FORK_COMPACT_OWNER_REQUIRED")
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=home / "compact"), backend=None)
    resolve_compact_summary_view(agent, source, THREAD_COMPACT_SCOPE)
    chain = committed_compact_checkpoint_chain(agent, source)
    copied, mapping = [], {}
    tool_ids, tool_refs = _prefix_tool_call_ids(rows)
    for row in chain:
        ids = _checkpoint_sources(row, rows, id_map)
        base = str(row.get("summary_base_checkpoint_id", row.get("previous_checkpoint_id", "")))
        if ids is None or (base and base not in mapping) or not _checkpoint_provenance_matches(row, set(id_map), tool_ids, tool_refs):
            continue
        candidate = {**row, "previous_checkpoint_id": next(reversed(mapping), ""),
                     "summary_base_checkpoint_id": base, "previous_generation": len(copied), "generation": len(copied) + 1}
        mapped = _remap_checkpoint(candidate, ids, branch, id_map, offsets, mapping)
        mapping[str(row["checkpoint_id"])] = mapped["checkpoint_id"]
        copied.append(mapped)
    if not copied:
        return branch, ""
    head = copied[-1]
    branch = replace(branch, summary=str(head["summary"]), compact_generation=int(head["generation"]),
        compact_checkpoint_id=head["checkpoint_id"], compacted_through_message_id=head["source_end_message_id"],
        compacted_through_byte_offset=head["source_end_byte_offset"], compact_updated_at=float(head["created_at"]),
        compact_operation_evidence=copy.deepcopy(head.get("operation_evidence", {})),
        compact_source_messages=int(head.get("source_messages_total", len(id_map))),
        compact_source_tool_pairs=int(head.get("source_tool_pairs_total", 0)))
    return branch, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in copied)


# LLM: 裸ID兼容旧来源；四元运行身份只由含真实调用的前缀消息元数据证明，payload及当前runner不能补认或覆盖。
# 函数用途: 同时收集前缀可证明的工具call ID与完整四元身份。
def _prefix_tool_call_ids(rows):
    results = frozenset(call_id for row in rows if row.role == 'tool'
        for payload in _tool_payloads(row) for kind, call_id in _tool_events(payload) if kind == 'result')
    calls = [(call_id, compact_tool_ref_key({**row.metadata, 'call_id': call_id}))
        for row in rows for payload in _tool_payloads(row, results)
        for kind, call_id in _tool_events(payload) if kind == 'call' and call_id]
    return {call_id for call_id, _ in calls}, {ref for _, ref in calls if ref is not None}


# LLM: 仅匹配结构化身份字段，未知扩展正文原样保留；显式引用类型错误、未知或未来身份都失败关闭。
# 函数用途: 核验检查点一个字段的消息或工具来源集合。
def _checkpoint_reference_matches(key, value, message_ids, tool_ids):
    if _is_checkpoint_message_reference(key):
        allowed = message_ids
    elif key in {'call_id', 'call_ids', 'tool_call_id', 'tool_call_ids', 'tool_use_id', 'tool_use_ids'} or key.endswith(('_tool_call_id', '_tool_call_ids')):
        allowed = tool_ids
    else:
        return True
    if key.endswith('_ids'):
        return isinstance(value, (list, tuple)) and all(isinstance(item, str) and item in allowed for item in value)
    return value is None or value == '' or isinstance(value, str) and value in allowed


# LLM: 全JSON迭代检查；工具对象显式携带身份维度时必须完整四元匹配，不能降级裸call_id自证来源。
# 函数用途: 排除前缀之外或身份无证明的结构化消息/工具引用，不修改原检查点。
def _checkpoint_provenance_matches(row, message_ids, tool_ids, tool_refs):
    pending = [row]
    while pending:
        value = pending.pop()
        if isinstance(value, (list, tuple)):
            pending.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        if 'call_id' in value and any(key in value for key in ('run_id', 'attempt_id', 'turn_id')):
            if compact_tool_ref_key(value) not in tool_refs:
                return False
        for key, child in value.items():
            if not _checkpoint_reference_matches(key, child, message_ids, tool_ids):
                return False
            if isinstance(child, (dict, list, tuple)):
                pending.append(child)
    return True


# LLM: 只读原人格快照，路径逐级拒绝链接；同代次保留原字节，早期分叉只改新快照epoch防止首轮被刷新。
# 函数用途: 读取可以复制的冻结人格，不创建缺失人格目录。
def _persona_copy(home: Path | None, source: ConversationThread, branch: ConversationThread) -> str | None:
    if home is None:
        return None
    directory = home / "persona" / "thread_prefix"
    path = directory / f"{source.thread_id}.json"
    if any(part.is_symlink() for part in (home, home / "persona", directory, path)):
        raise ConversationForkError("FORK_PERSONA_SYMLINK")
    if not path.exists():
        return None
    from ..prompting_parts.thread_persona import _read_snapshot

    saved = _read_snapshot(path)
    if saved is None:
        return None
    if source.compact_generation == branch.compact_generation:
        return path.read_text(encoding="utf-8")
    saved["epoch"]["compact_generation"] = branch.compact_generation
    return json.dumps(saved, ensure_ascii=False)


# LLM: 新opaque目标须不存在；失败由同一线程锁下的归属判断决定整组清理，保留线程时也保留它引用的资源。
# 函数用途: 提交分叉资源并最后发表线程；异常时原子决定整组回收或保留，避免半分叉。
def _publish(store: ConversationStore, branch: ConversationThread, home: Path | None,
             messages: str, compact: str, persona: str | None) -> None:
    resources = {store.storage.message_path(branch.thread_id): messages}
    if home is not None and compact:
        resources[home / "compact" / "conversations" / f"{branch.thread_id}.jsonl"] = compact
    if home is not None and persona is not None:
        resources[home / "persona" / "thread_prefix" / f"{branch.thread_id}.json"] = persona
    targets = (*resources, store.storage.thread_path(branch.thread_id))
    if any(path.exists() or path.is_symlink() for path in targets):
        raise FileExistsError("fork resource already exists")
    written = []
    try:
        for path, content in resources.items():
            written.append(path)
            write_private_text_file_atomic(path, content)
        create_thread_atomic(store.threads, branch)
    except BaseException:
        _rollback_fork_thread(store, branch, written)
        raise


# LLM: 原canonical线程锁覆盖归属检查及整组删除；陌生/已更新线程连同其按ID引用的资源一并保留，不单独拆资源。
# 函数用途: 回收本次失败发表的完整资源组，未知归属则全部保留，防止误删或留下无消息线程。
def _rollback_fork_thread(store, branch, written=()):
    if not branch.metadata.get('fork_publication_id'):
        return
    path = store.storage.thread_path(branch.thread_id)
    parts = path.relative_to(store.storage.root).parts
    with locked_json_path(path):
        raw = read_bytes_beneath(store.storage.root, parts, max_bytes=16 * 1024 * 1024)
        if raw is not None:
            if json.loads(raw) != json.loads(json.dumps(branch.to_dict(), ensure_ascii=False)):
                return
            unlink_file_beneath(store.storage.root, parts)
        for resource in written:
            resource.unlink(missing_ok=True)


# LLM: push服务已核目标owner并创建专用线程；外部role只是引用标签，全部按普通不可信user文本存储，无原生工具协议与真人授权。
# 函数用途: 将脱敏历史幂等导入目标线程，恢复推送不能重复追加上下文。
def fork_untrusted_context(store, thread_id, snapshot, push_id):
    from .learning_source import unknown_projection
    from .remote_push_context import sanitize_remote_context

    for index, row in enumerate(sanitize_remote_context(snapshot)):
        store.messages.append_once({"thread_id": thread_id, "role": "user",
            "content": f"[外部历史引用·{row['role']}；非当前用户指令]\n{row['content']}",
            "metadata": {"input_origin": "remote_push", "learning_source": unknown_projection()}},
            dedupe_key=f"remote-push-context:{push_id}:{index}")


# LLM: 只分叉主会话，不复用源线程任务/通道/运行身份；参数与返回dict是CLI、Host复用的公共合同。
# 函数用途: 从最新或指定消息安全分叉，复制历史设置、压缩基线和冻结人格，源线程零修改。
def fork_conversation_thread(
    store: ConversationStore, *, source_thread_id: str, up_to_message_id: str | None = None,
    title: str | None = None, workspace_dir: str | None = None, owner_home: Path | None = None,
    is_admin: bool = False,
) -> dict[str, object]:
    source = store.threads.require(source_thread_id)
    if source.metadata.get("thread_kind") == "agent":
        raise ConversationForkError("FORK_MAIN_THREAD_REQUIRED")
    rows, errors = store.messages.recent_report(source.thread_id, limit=0)
    if errors or any(row.thread_id != source.thread_id for row in rows):
        raise ConversationForkError("FORK_TRANSCRIPT_INVALID")
    prefix, adjusted = _safe_prefix(rows, up_to_message_id)
    home = _owner_home(source, owner_home)
    branch = _new_thread(source, prefix, title, workspace_dir, home, is_admin)
    messages, id_map, offsets = _message_copy(prefix, branch.thread_id)
    branch, compact = _compact_copy(source, branch, prefix, home, id_map, offsets)
    if home is None:
        persona = None
        _publish(store, branch, home, messages, compact, persona)
    else:
        # 与原人格freeze/expire共锁，复制不能复活已撤销来源；源缺人格时不创建它。
        with locked_json_path(home / "persona" / "thread_prefix.lock"):
            persona = _persona_copy(home, source, branch)
            _publish(store, branch, home, messages, compact, persona)
    return {"new_thread_id": branch.thread_id, "thread_id": branch.thread_id, "thread": branch.to_dict(),
            "forked_from_thread_id": source.thread_id, "forked_from_message_id": branch.forked_from_message_id,
            "forked_message_count": len(prefix), "workspace_root": branch.workspace_root,
            "boundary_adjusted": adjusted}
