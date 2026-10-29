# LLM: T外只操作私有state和结果；commit复用原宿主checkpoint及有限/无界收尾判据，不另建plan账。
# 模块用途: 分开长读取与原事务提交，保持两条采集入口边界语义一致。
from __future__ import annotations

import time
from copy import deepcopy

from . import harvester as harvest
from .puller import SOURCE_FETCH_TIMEOUT_SECONDS, DrainBudget
from .sources import apply_cursor_page_feedback, persist_file_fragment
from .watch_feedback import consume_feedback_inbox
from .watch_payloads import build_audit_record
from .watch_state import audit_append, persist_state


# LLM: 复用同一固定上界drain；feedback/去重/engine只变私有state，背压不联网，不在T外写任何原事务文件。
# 函数用途: 在原T和state.lock外完成长网络与候选计算。
def prepare_harvest_plan(plan, fetch_json):
    state = plan.working
    if harvest._backpressured(state):
        key = 'disk_backpressure_skips' if state.audit_guarantee else 'backpressure_skips'
        state.totals[key] = int(state.totals.get(key, 0) or 0) + 1
        return harvest.DrainResult(cursor=state.cursor, aux_cursor=state.line_cursor,
                                   source_checkpoint=deepcopy(state.source_checkpoint)), []
    consume_feedback_inbox(state, time.time())
    content_mode = harvest.is_content_mode(state)
    budget = DrainBudget(max_events=harvest._cycle_drain_events(state, content_mode),
                        page_limit=state.tuning.page_limit, deadline=time.time() + SOURCE_FETCH_TIMEOUT_SECONDS)
    drain = harvest._drain_harvest_source(state, fetch_json, budget, finalize_window=plan.finalize_window)
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


# LLM: 原T/state.lock/lease/intent门不变；来源checkpoint保住宿主键，有限追平或无界末拍成功才收尾，错误待重试。
# 函数用途: 按原顺序提交记录与游标，并保存准确的complete/partial窗口终态。
def commit_harvest_plan(plan, drain, chunks):
    state, transaction = plan.working, plan.transaction
    harvest._assert_active_harvest_transaction(state, transaction)
    keys = transaction.get('source_record_keys')
    if keys:
        harvest._stage_harvest_source_record_keys(state, transaction, keys)
    if not drain.error and not persist_file_fragment(state, drain):
        raise OSError('source fragment persistence failed')
    if drain.error:
        harvest._record_harvest_schedule(state, drain)
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
        state.source_checkpoint = harvest._merge_harvest_checkpoint(state, drain.source_checkpoint)
    state.line_cursor = int(getattr(drain, 'aux_cursor', 0) or 0)
    if float(getattr(drain, 'fetched_at', 0) or 0) > 0:
        state.last_poll_at = float(drain.fetched_at)
    state.last_reached_end = drain.reached_end
    state.totals['gap_events'] += drain.gap_events
    if plan.finalize_window:
        harvest._finalize_harvest_window(state, drain)
    harvest._record_harvest_schedule(state, drain)
    harvest._update_ingest_throughput(state, now=time.time())
    harvest._commit_harvest_transaction(state, transaction)
    generation = state.spool_generation
    harvest._maybe_rotate_spool(state, harvest.spool_path(state))
    if state.spool_generation != generation:
        persist_state(state)
    return True
