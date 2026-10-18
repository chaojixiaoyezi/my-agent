# LLM: T外仅操作一次收割的私有state/DrainResult/引擎结果；物理写只由原Gateway短门调用commit，不持久化第二plan账。
# 模块用途: 分开网络及长扫描与原spool/audit/cursor事务，复用原结构化来源和引擎，不添加业务判定。
from __future__ import annotations

import time
from copy import deepcopy

from . import harvester as harvest
from .puller import SOURCE_FETCH_TIMEOUT_SECONDS, DrainBudget
from .sources import apply_cursor_page_feedback, drain_watch_source, persist_file_fragment
from .watch_feedback import consume_feedback_inbox
from .watch_payloads import build_audit_record
from .watch_state import audit_append, persist_state


# LLM: 本次source仍由既有统一drain解释，feedback/去重/engine都只变私有state；无文件写，不继承前一请求的抓取闭包。
# 函数用途: 在原T和state.lock外完成长网络与候选计算。
def prepare_harvest_plan(plan, fetch_json):
    state = plan.working
    consume_feedback_inbox(state, time.time())
    content_mode = harvest.is_content_mode(state)
    budget = DrainBudget(max_events=harvest._cycle_drain_events(state, content_mode),
                        page_limit=state.tuning.page_limit, deadline=time.time() + SOURCE_FETCH_TIMEOUT_SECONDS)
    drain = drain_watch_source(state, fetch_json, budget, force_poll=plan.finalize_window)
    apply_cursor_page_feedback(state, drain)
    if not drain.error:
        harvest._deduplicate_source_records(state, drain, plan.transaction, persist=False)
    state.totals['pulls'] += 1
    if drain.error:
        state.totals['http_errors'] += 1
        state.last_error, state.last_error_code = drain.error, drain.error_code or 'NETWORK_REQUEST_FAILED'
        return drain, []
    state.last_error, state.last_error_code = '', ''
    chunk_size = harvest.content_batch_size(state.tuning) if content_mode else int(state.tuning.harvest_chunk_events or 0)
    chunks = []
    headroom = harvest.judge_headroom(state)
    for index, chunk in enumerate(harvest._event_chunks(drain.events, chunk_size)):
        view = harvest._chunk_drain_view(drain, chunk, first=index == 0)
        digest = state.engine.process(chunk, time.time(), judge_headroom=headroom,
                                      cold_start=state.source_spec is None, guarantee=state.audit_guarantee)
        headroom = max(0, headroom - len(digest.candidates))
        chunks.append((view, digest, build_audit_record(view, digest)))
    return drain, chunks


# LLM: Caller已持原T→实际state.lock→lease/intent门；写所有原文件后才发布游标，失败留原intent，不靠cursor猜完成。
# 函数用途: 将一拍计算结果按原事务顺序写到fragment、spool、audit及唯一state快照。
def commit_harvest_plan(plan, drain, chunks):
    state, transaction = plan.working, plan.transaction
    harvest._assert_active_harvest_transaction(state, transaction)
    keys = transaction.get('source_record_keys')
    if keys:
        harvest._stage_harvest_source_record_keys(state, transaction, keys)
    if not drain.error and not persist_file_fragment(state, drain):
        raise OSError('source fragment persistence failed')
    if drain.error:
        harvest._commit_harvest_transaction(state, transaction)
        return False
    for view, digest, record in chunks:
        harvest._assert_active_harvest_transaction(state, transaction)
        if digest.candidates:
            harvest._spool_append(state, view, digest)
        if not audit_append(state, record):
            raise OSError('audit ledger append failed')
    state.cursor = drain.cursor
    if isinstance(drain.source_checkpoint, dict):
        state.source_checkpoint = deepcopy(drain.source_checkpoint)
    state.line_cursor = int(getattr(drain, 'aux_cursor', 0) or 0)
    if float(getattr(drain, 'fetched_at', 0) or 0) > 0:
        state.last_poll_at = float(drain.fetched_at)
    state.last_reached_end = drain.reached_end
    state.totals['gap_events'] += drain.gap_events
    if plan.finalize_window:
        state.window_finalized_at = time.time()
    harvest._update_ingest_throughput(state, now=time.time())
    harvest._commit_harvest_transaction(state, transaction)
    generation = state.spool_generation
    harvest._maybe_rotate_spool(state, harvest.spool_path(state))
    if state.spool_generation != generation:
        persist_state(state)
    return True
