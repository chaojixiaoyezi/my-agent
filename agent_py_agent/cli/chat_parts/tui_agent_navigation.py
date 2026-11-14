"""Typed navigation state for entering and controlling delegated-agent views."""

# LLM: 本模块只拥有 TUI 选择、视图栈、各页展示 runtime 与游标；运行/消息/取消权威始终留在 Gateway 领域层。
# 结束页正文必须保留原 thread/message ID，不能以正文哈希或 legacy 文案创建身份。
# 模块用途: 空输入方向键先选后台观察、再选 Goal/子代理；观察页面只读，返回不控制生命周期。

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ...agent.conversation.background_process_view import background_watch_rows
from .tui_runtime import _BACKGROUND_TRANSCRIPT_SCHEMA as BACKGROUND_TRANSCRIPT_SCHEMA
from .tui_runtime import TuiRuntime

_TERMINAL_AGENT_STATUSES = frozenset(
    {
        "DONE",
        "FAILED",
        "BLOCKED",
        "CHANNEL_ERROR",
        "TIMEOUT",
        "CANCELLED",
        "ABANDONED",
        "TAKEN_OVER",
    }
)
_AGENT_STARTUP_NOTICE_KIND = "agent_startup"
_ROW_SCALAR_FIELDS = frozenset(
    {
        "run_id",
        "root_task_id",
        "parent_run_id",
        "depth",
        "name",
        "role",
        "status",
        "lifecycle_phase",
        "description",
        "goal",
        "activity",
        "attempts",
        "context_tokens",
        "compact_count",
        "created_at",
        "updated_at",
        "heartbeat_at",
        "ended_at",
        # 已被接替时 Gateway 摊平的 kernel replaced_by 投影（接替者 run_id 与处置），子代理页头部据此标注。
        "replaced_by_run_id",
        "replaced_by_disposition",
        # Gateway 用 runner_display_projection 算好的失败类型专属标签（宿主停机中断、额度不足等），客户端不另写映射。
        "failure_label",
    }
)
_GOAL_ROW_SCALAR_FIELDS = frozenset(
    {
        "goal_id",
        "revision",
        "name",
        "objective",
        "status",
        "tokens_used",
        "token_budget",
        "time_used_seconds",
        "duration_seconds",
        "created_at",
        "updated_at",
    }
)


# LLM: 快照是渲染与按键的只读边界，只含准确 run 身份和展示标量；后台页面没有代理控制身份。
# 类用途: 告诉界面当前看的是谁、选中了谁，以及是否处于不能输入的后台观察页。
@dataclass(frozen=True)
class TuiAgentNavigationSnapshot:
    active_run_id: str = ""
    active_name: str = "main"
    active_status: str = ""
    selected_run_id: str = ""
    expanded_goal_id: str = ""
    depth: int = 0
    terminal: bool = False
    # 当前子代理已被接替时的接替者 run_id（来自行里的 replaced_by_run_id），头部显示“已被 X 接替”。
    active_replaced_by_run_id: str = ""
    # 当前子代理的失败类型专属标签（来自行里的 failure_label），头部徽标优先显示它。
    active_failure_label: str = ""
    background_process_count: int = 0
    background_watch_active: bool = False
    can_send_input: bool = True
    active_thread_id: str = ""
    business_list_active: bool = False
    business_agents_enabled: bool = False
    background_agent_count: int | None = None
    background_agent_running_count: int | None = None
    business_space_id: str = ''
    active_agent_id: str = ''
    active_identity_state: str = ''
    active_work_state: str = ''
    route_enabled: bool | None = None


# LLM: This immutable projection is the only normalized input accepted by the child
# view updater. It contains display facts, not control authority or executable objects.
# 类用途: 保存一次子代理详情响应中已经校验过的行、事件和终态展示字段。
@dataclass(frozen=True)
class _AgentViewProjection:
    run_id: str
    payload: Mapping[str, object]
    raw_agent: Mapping[str, object]
    row: dict[str, object]
    children: tuple[dict[str, object], ...]
    events: object
    typed_final_request_ids: frozenset[str]
    final_response: str
    final_response_request_id: str
    final_response_key: str
    terminal: bool
    status: str
    lifecycle_phase: str


# LLM: 每个 TUI 共用一个导航实例和根 runtime；子页与进程页只保存显示状态，不能执行模型或控制进程。
# 类用途: 管理代理视图栈及只读进程观察页，进程句柄永不成为子代理控制身份。
class TuiAgentNavigationState:
    # LLM: 初始化固定根 runtime 和有界进程投影；换会话必须新建导航，不携带旧会话控制身份。
    # 函数用途: 创建主代理导航栈和仅用于展示的后台行缓存。
    def __init__(self, root_runtime: TuiRuntime, *, max_cached_runtimes: int = 32) -> None:
        if not isinstance(root_runtime, TuiRuntime):
            raise TypeError("root_runtime must be TuiRuntime")
        self.root_runtime = root_runtime
        self._max_cached_runtimes = max(1, int(max_cached_runtimes))
        self._runtimes: dict[str, TuiRuntime] = {}
        self._rows_by_parent: dict[str, tuple[dict[str, object], ...]] = {"": ()}
        self._rows_by_run: dict[str, dict[str, object]] = {}
        self._goal_rows: dict[str, dict[str, object]] = {}
        self._child_goal_rows: dict[str, dict[str, dict[str, object]]] = {}
        self._selected_by_parent: dict[str, str] = {}
        self._expanded_goal_id = ""
        self._path: list[str] = []
        self._background_rows: tuple[dict, ...] = ()
        self._background_count = 0
        self._event_cursors: dict[str, int] = {}
        self._goal_published: set[str] = set()
        self._final_response_keys: dict[str, str] = {}
        self._typed_final_response_request_ids: set[str] = set()
        self._view_change: Callable[[TuiRuntime], None] | None = None
        self._lock = threading.RLock()
        self.business_agents = None
        self._business_loading: set[str] = set()
        self._business_messages: dict[str, dict[str, None]] = {}
        self._business_targets: dict[str, dict] = {}
        self._business_history: dict[str, dict] = {}
        self._business_tools: dict[str, dict[str, str]] = {}
        self._business_visible: tuple | None = None
        self.route_enabled: bool | None = None

    # LLM: route是共享会话设置的只读投影，未知不显示false，也不影响观察权限。
    # 函数用途: 更新底栏模式，不改变工作生命周期。
    def update_route_setting(self, setting: object) -> None:
        with self._lock:
            enabled = setting.get('enabled') if isinstance(setting, dict) else None
            enabled = enabled if isinstance(enabled, bool) else None
            changed, self.route_enabled = enabled != self.route_enabled, enabled
        # 未变化的周期观察不清渲染缓存、不重置刷新节奏；变化才整屏重绘。
        if changed:
            self.root_runtime.store.invalidate()

    # LLM: 名册仅作显示；thread_id不进入原run控制链，权限撤销清除全部业务会话。
    # 函数用途: 更新业务聚合状态与可见会话。
    def update_business_agents(self, projection) -> None:
        """业务名册仅为显示投影；thread_id 不进入原 run 控制链。"""
        with self._lock:
            self.business_agents = projection
            latest = {row['agent_id']: row for row in projection.rows}
            for key, target in self._business_targets.items():
                observed = latest.get(target['agent_id'])
                if (observed is not None and observed.get('space_id') == target.get('space_id')
                        and observed.get('thread_id', target['thread_id']) == target['thread_id']):
                    self._business_targets[key] = {**target, **observed}
            if projection.read_denied:
                for key, target in self._business_targets.items():
                    self._runtimes[key].publish_conversation_reset(target['thread_id'])
                self._business_messages.clear()
                self._business_history.clear()
                self._business_tools.clear()
            listing = self._business_listing_locked()
            if listing:
                self._publish_business_locked()
            page = self._path[-1] if self._path else ''
            thread_id = self._business_targets.get(page, {}).get('thread_id', '')
            runtime = self._active_runtime_locked()
            visible = (projection.count, projection.running_count, projection.coverage, projection.read_denied,
                       tuple((row.get('agent_id'), row.get('work_state')) for row in projection.rows))
            changed, self._business_visible = visible != self._business_visible, visible
        if thread_id and not projection.read_denied:
            self._load_business_conversation(thread_id, runtime)
        # 名册底栏投影不变且未打开业务页时，周期刷新不清渲染缓存、不重置刷新节奏。
        if changed or listing or thread_id:
            self.root_runtime.store.invalidate()

    # LLM: 沿用同一导航栈和renderer，只追加空间层与后台实体数据。
    # 函数用途: 判断栈顶是否业务根或空间列表。
    def _business_listing_locked(self) -> bool:
        return bool(self._path and (self._path[-1] == 'business:agents' or self._path[-1].startswith('business:space:')))

    # LLM: 选择按准确节点ID保留，不因筛选改写后台身份。
    # 函数用途: 将当前层行发布到原background_activity显示块。
    def _publish_business_locked(self) -> None:
        page = self._path[-1]
        space_id = page.removeprefix('business:space:') if page.startswith('business:space:') else None
        runtime = self._runtime_for_locked(page)
        selected = self._selected_by_parent.get(page, '')
        if selected not in self._selection_ids_locked(page):
            self._selected_by_parent.pop(page, None)
            selected = ''
        block_id = 'business:team-list'
        exists = any(block.block_id == block_id for block in runtime.store.snapshot().active_blocks)
        runtime._publish('background_activity_updated' if exists else 'background_activity_started',
                         'updated' if exists else 'started', block_id,
                         {'source_kind': 'business_agents', 'display': {'lines': self.business_agents.lines(selected, space_id=space_id)}})

    # LLM: Enter仅使用已观察的稳定节点，不从名称推断目标或创建任务。
    # 函数用途: 展开根、空间、续页或准确后台会话。
    def _enter_business_selected(self) -> bool:
        with self._lock:
            if self.business_agents is None:
                return False
            page = self._path[-1] if self._path else ''
            listing = self._business_listing_locked()
            selected = self._selected_by_parent.get(page if listing else '', '')
            if not listing and (page or selected != 'business:agents'):
                return False
            thread_id = ''
            space_id = page.removeprefix('business:space:') if page.startswith('business:space:') else None
            if listing:
                row = next((row for row in self.business_agents.navigation_rows(space_id) if row['navigation_id'] == selected), None)
                if row is None:
                    return False
                if row['kind'] == 'space':
                    self._path.append(row['navigation_id'])
                elif row['kind'] == 'background_main':
                    thread_id = row.get('thread_id') or row['agent_id']
                    key = 'business:' + thread_id
                    self._business_targets[key] = {**row, 'thread_id': thread_id}
                    self._path.append(key)
            else:
                row = None
                self._path.append('business:agents')
        if row is None:
            self.business_agents.load_root()
        elif row['kind'] == 'space':
            self.business_agents.load_space(row['space_id'])
        elif row['kind'] == 'page':
            self.business_agents.load_more(space_id, row['page_kind'])
        with self._lock:
            if self._business_listing_locked():
                self._publish_business_locked()
            runtime, callback = self._active_runtime_locked(), self._view_change
        if callback is not None:
            callback(runtime)
        if thread_id:
            self._load_business_conversation(thread_id, runtime)
        runtime.store.invalidate()
        return True

    # LLM: 每次最多一页；刷新无游标读当前页，只有/history推进旧历史续点，失败不消费续点。
    # 函数用途: 独立维护实时观察和旧历史分页，保留原message_id与准确agent/thread归属。
    def _load_business_conversation(self, thread_id: str, runtime: TuiRuntime, *, more: bool = False) -> None:
        with self._lock:
            if thread_id in self._business_loading:
                return
            history = self._business_history.get(thread_id, {})
            cursor = history.get('next_cursor') if more else None
            if more and not cursor:
                return
            target = self._business_targets.get('business:' + thread_id)
            if target is None:
                return
            projection = self.business_agents
            generation = runtime.conversation_reset_generation
            self._business_loading.add(thread_id)

        # LLM: 调用方持导航锁并经原runtime reset围栏；切页不改归属，替换观察源或重置后旧页不再发表。
        # 函数用途: 去重发表一页正文，独立保存旧历史续点和真实覆盖提示。
        def publish_page(result):
            if self.business_agents is not projection:
                return
            if projection.read_denied or result.get('read_denied'):
                runtime.publish_conversation_reset(thread_id)
                self._business_messages.pop(thread_id, None)
                self._business_history.pop(thread_id, None)
                self._business_tools.pop(thread_id, None)
                return
            seen = self._business_messages.setdefault(thread_id, {})
            # 只核本页至多100个ID；整页与已见消息不衔接时，保留跨页新增产生的未读缺口。
            history_gap = (not more and bool(result.get('next_cursor') and result['items'])
                           and not any(row['message_id'] in seen for row in result['items']))

            def _row_to_event(row):
                msg_id = str(row.get('message_id') or '')
                role = str(row.get('role') or 'user')
                text = str(row.get('text') or '')
                req_id = f"history:{thread_id}:{msg_id}"
                block_id = f"history:{thread_id}:{msg_id}:{role}"
                kind_map = {
                    'user': 'user_message',
                    'assistant': 'assistant_completed',
                    'tool': 'tool_completed',
                    'host': 'system_message',
                }
                kind = kind_map.get(role, 'user_message')
                payload = {'text': text, 'message_id': msg_id, 'thread_id': thread_id}
                if role == 'tool':
                    tool_name = str(row.get('tool_name') or 'tool')
                    payload['tool'] = tool_name
                    payload['title'] = tool_name
                    payload['detail'] = text
                return {
                    'schema': 'conversation_history_display.v1',
                    'kind': kind,
                    'phase': 'completed',
                    'request_id': req_id,
                    'block_id': block_id,
                    'payload': payload,
                    'occurred_at': row.get('occurred_at'),
                }

            page_events = []
            for row in result.get('items', []):
                if row.get('message_id') and row['message_id'] not in seen:
                    page_events.append(_row_to_event(row))
                    seen[row['message_id']] = None
                    while len(seen) > 2048:
                        seen.pop(next(iter(seen)))

            # 原checkpoint事件沿同一renderer/block ID恢复；工具继续由唯一操作账显示。
            for event in result.get('display_events', [])[:100]:
                if not isinstance(event, dict):
                    continue
                payload = event.get('payload')
                if not isinstance(payload, dict):
                    continue
                if event.get('kind') == 'system_message':
                    from ...agent.conversation.background_transcript import (
                        public_workflow_step_display,
                    )

                    step = public_workflow_step_display(payload.get('workflow_step'))
                    if (step is None or event.get('thread_id') != thread_id
                            or event.get('phase') != 'completed'):
                        continue
                    event, payload = {**event, 'payload': step}, step
                elif event.get('kind') != 'assistant_completed':
                    continue
                canonical = payload.get('canonical_message_id')
                if canonical and canonical in seen:
                    continue
                page_events.append(event)
                if canonical:
                    seen[canonical] = None
                    while len(seen) > 2048:
                        seen.pop(next(iter(seen)))

            if page_events:
                page_events.sort(key=lambda event: str(event.get('occurred_at') or ''))
                previous_ids = {b.block_id for b in runtime.store.snapshot().stable_blocks}
                runtime._publish_recovered_display_events(tuple(page_events))
                if more:
                    inserted_ids = [
                        b.block_id for b in runtime.store.snapshot().stable_blocks
                        if b.block_id not in previous_ids
                    ]
                    if inserted_ids:
                        runtime._publish('history_page_prepended', 'completed',
                                         f"history-page:{thread_id}:{cursor or len(seen)}",
                                         {'block_ids': inserted_ids})

            if result.get('coverage') == 'unknown':
                err = result.get('error') if isinstance(result.get('error'), dict) else {}
                err_code = result.get('error_code') or err.get('code') or 'LOAD_FAILED'
                history = self._business_history.setdefault(thread_id, {'next_cursor': None})
                diagnostic = json.dumps({'code': err_code, 'details': err.get('details', []),
                    'effect_outcome': result.get('effect_outcome', 'unknown')}, ensure_ascii=False, sort_keys=True)
                if len(diagnostic) > 8192:
                    diagnostic = diagnostic[:8192] + '…（诊断预览已截断，请保留原错误码核查服务回执）'
                if history.get('read_error_signature') != diagnostic:
                    runtime.write_console('历史读取失败（只读） · request_id='
                        + str(result.get('request_id') or 'unknown')[:128] + '：' + diagnostic
                        + '；核查后可 /history 重试；游标过期请 Ctrl+G 返回再进入以读取新续点。')
                history.update(read_error_code=err_code, read_error_signature=diagnostic)
                runtime.set_notice(f'历史不可读（coverage=unknown）：{err_code} · 详细原因见本页诊断；/history 重试',
                    duration_seconds=8.0, notice_kind='warning')
                return

            # 正常刷新不消费旧续点；跨页缺口只重建更近的历史入口，更老未读页仍在其后方可达。
            if result.get('coverage') != 'unknown' and (more or history_gap or thread_id not in self._business_history
                    or not more and self._business_history.get(thread_id, {}).get('read_error_code') == 'CURSOR_EXPIRED'):
                self._business_history[thread_id] = {'next_cursor': result.get('next_cursor')}
            self._business_history.get(thread_id, {}).pop('read_error_code', None)
            self._business_history.get(thread_id, {}).pop('read_error_signature', None)
            next_cursor = self._business_history.get(thread_id, {}).get('next_cursor')
            continuation = ' · /history 加载下一页' if next_cursor else ''
            boundary = result.get('history_boundary')
            reason = boundary.get('reason_code') if isinstance(boundary, dict) else None
            label = {'CANONICAL_HISTORY_NOT_RECORDED': '未记录', 'CANONICAL_HISTORY_EMPTY': '已记录空历史',
                     'CANONICAL_HISTORY_UNREADABLE': '历史不可读', 'NO_PUBLIC_MESSAGES_IN_PAGE': '本页无公开消息'}.get(reason, reason or '边界未知')
            runtime.set_notice('会话覆盖：' + str(result.get('coverage', 'unknown')) + ' · ' + str(label)
                + continuation + ' · /help 命令 · Ctrl+G 返回', duration_seconds=6)

        # LLM: I/O在导航锁外；成功/失败显示均绑定原reset代次，finally始终释放本thread的加载标记。
        # 函数用途: 读取一个有界页，再通过原runtime合同发表或丢弃迟到结果。
        def load():
            try:
                result = projection.conversation(target['agent_id'], thread_id=thread_id, cursor=cursor)
                activity = (projection.execution_activity(target['agent_id'], thread_id=thread_id,
                    space_id=target.get('space_id')) if not more and hasattr(projection, 'execution_activity') else None)
                with self._lock:
                    runtime.apply_conversation_snapshot(generation, lambda: (publish_page(result),
                        self._publish_business_activity(thread_id, runtime, activity) if activity is not None
                        and not projection.read_denied and not result.get('read_denied') else None))
            except Exception as exc:
                err_code = type(exc).__name__
                with self._lock:
                    runtime.apply_conversation_snapshot(generation, lambda: runtime.set_notice(
                        f'完整会话读取失败（{err_code}），coverage=unknown；可执行 /history 重试或按 Ctrl+G 返回主会话',
                        duration_seconds=8.0, notice_kind='warning'))
            finally:
                with self._lock:
                    self._business_loading.discard(thread_id)
                runtime.store.invalidate()

        threading.Thread(target=load, name='tui-business-history', daemon=True).start()

    def _publish_business_activity(self, thread_id: str, runtime: TuiRuntime, activity: dict) -> None:
        """同原聊天runtime发表真实operation状态块；只保留有界去重窗口，不派活。"""
        import json

        seen = self._business_tools.setdefault(thread_id, {})
        for row in activity.get('tools', ())[:160]:
            key = ':'.join(str(row.get(field) or '') for field in ('run_id', 'attempt_id', 'operation_id'))
            fingerprint = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if seen.get(key) == fingerprint:
                continue
            status = row.get('status')
            error = row.get('error') if isinstance(row.get('error'), dict) else {}
            detail = ' · '.join(str(value) for value in (row['run_id'], status,
                '主体已启动' if row.get('executed') is True else '主体未启动' if row.get('executed') is False else '主体启动未知',
                row.get('effect_outcome'), error.get('code'), error.get('message')) if value)
            block_id = 'business-operation:' + key
            request_id = 'business-execution:' + row['run_id']
            if status == 'SUCCEEDED':
                kind, phase, ok = 'tool_completed', 'completed', True
            elif status in {'FAILED', 'CANCELLED'}:
                kind, phase, ok = 'tool_failed', 'failed', False
            elif status == 'EXECUTING' and row.get('executed') is True and row.get('effect_outcome') == 'running':
                kind, phase, ok = 'tool_started', 'started', None
            else:
                # 这里只结束本地观察动画，不伪造工具成功/失败；未知执行仍保留为待核。
                if any(block.block_id == block_id for block in runtime.store.snapshot().active_blocks):
                    runtime._publish('background_activity_completed', 'completed', block_id, {}, request_id=request_id)
                label = ('排队（工具主体未启动）' if status == 'CLAIMED' and row.get('executed') is False
                         else '结果待核' if status == 'UNKNOWN' or row.get('effect_outcome') == 'unknown'
                         else '执行状态待核')
                runtime._publish('system_message', 'completed', block_id + ':state:' + str(status),
                    {'text': row['tool_name'] + ' · ' + label + ' · ' + detail,
                     'operation_id': row['operation_id'], 'run_id': row['run_id'],
                     'attempt_id': row['attempt_id'], 'observed_status': status}, request_id=request_id)
                kind = ''
            if kind:
                runtime._publish(kind, phase, block_id,
                    {'tool': row['tool_name'], 'title': row['tool_name'], 'detail': detail,
                     'ok': ok, 'source_kind': 'business_operation', 'invocation': row.get('invocation') or '',
                     'operation_id': row['operation_id'], 'run_id': row['run_id'],
                     'attempt_id': row['attempt_id'], 'error_code': error.get('code')}, request_id=request_id)
            seen[key] = fingerprint
            while len(seen) > 256:
                seen.pop(next(iter(seen)))
        children = activity.get('children', ())[:50]
        lines = ['执行过程 · 覆盖 ' + str(activity.get('coverage', 'partial')),
                 '/details 按需查看调度阻拦、直属子代理与用量费用']
        current = activity.get('current_execution')
        if isinstance(current, dict):
            lines.append('当前执行 ' + str(current.get('run_id')) + ' · 尝试 ' + str(current.get('attempt_id')))
        lines.extend('直属子代理 ' + str(row.get('run_id')) + ' · ' + str(row.get('status', 'unknown'))
                     for row in children)
        if not children:
            lines.append('无直属子代理' if activity.get('coverage') == 'complete' else '直属子代理资料不完整')
        lines.extend(str(error.get('code', 'UNKNOWN')) + ' · ' + str(error.get('message', ''))
                     for error in activity.get('errors', ()) if isinstance(error, dict))
        block = 'business-execution-coverage:' + thread_id
        exists = any(row.block_id == block for row in runtime.store.snapshot().active_blocks)
        runtime._publish('background_activity_updated' if exists else 'background_activity_started',
                         'updated' if exists else 'started', block,
                         {'display': {'lines': tuple(lines)}, 'source_kind': 'business_execution'})

    # LLM: 此入口仅推进可见会话，不后台展开其它Agent历史。
    # 函数用途: 让/history显式读取下一页原生完整消息。
    def load_more_business_messages(self) -> bool:
        with self._lock:
            page = self._path[-1] if self._path else ''
            target = self._business_targets.get(page)
            if target is None:
                return False
            thread_id = target['thread_id']
            runtime = self._active_runtime_locked()
            history = self._business_history.get(thread_id, {})
            retry_current = bool(history.get('read_error_code') and not history.get('next_cursor'))
        self._load_business_conversation(thread_id, runtime, more=not retry_current)
        return True

    # LLM: 只更新有界显示行，不发信号、不改变运行；旧选择按准确 session 身份保留。
    # 函数用途: 刷新后台驻留徽标和已经打开的实时观察页。
    def update_background_watch_rows(self, value: object, *, active_count: int | None = None) -> bool:
        rows = background_watch_rows(value)
        count = sum(row.get("alive") is True or row.get("wake_pending") is True for row in rows) if active_count is None else max(0, int(active_count))
        with self._lock:
            if rows == self._background_rows and count == self._background_count:
                return False
            self._background_rows = rows
            self._background_count = count
            if self._selected_by_parent.get("") == "bg:watch" and not count:
                self._selected_by_parent.pop("", None)
            if self._path and self._path[-1] == "bg:watch":
                self._publish_watch_locked()
        self.root_runtime.store.invalidate()
        return True

    # LLM: 只写独立 TUI runtime 的可移除 display 块，不写 ConversationStore 或后台事件游标。
    # 函数用途: 更新观察页内容；上下键只切换哪一项日志展开。
    def _publish_watch_locked(self) -> None:
        runtime = self._runtime_for_locked("bg:watch")
        ids = [f"bg:{row['session_id']}" for row in self._background_rows]
        selected = self._selected_by_parent.get("bg:watch", "")
        if selected not in ids:
            selected = ids[0] if ids else ""
            self._selected_by_parent["bg:watch"] = selected
        block_id = "background:watch"
        exists = any(block.block_id == block_id for block in runtime.store.snapshot().active_blocks)
        runtime._publish("background_activity_updated" if exists else "background_activity_started", "updated" if exists else "started", block_id,
            {"source_kind": "background_watch", "display": {"rows": self._background_rows, "selected": selected, "active_count": self._background_count}})

    # LLM: The callback swaps only the visible state store/provider. It is
    # invoked outside the navigation lock to avoid UI re-entry deadlocks.
    # 函数用途: 绑定进入或返回代理时的界面切换动作。
    def set_view_change_callback(
        self,
        callback: Callable[[TuiRuntime], None] | None,
    ) -> None:
        with self._lock:
            self._view_change = callback

    # LLM: Root and child pollers replace one exact parent's row projection.
    # Selection follows run identity across reorder and clears if the target vanished.
    # 函数用途: 更新某一层直属子代理名册，并保持当前选中项稳定。
    def update_rows(self, parent_run_id: str, value: object) -> bool:
        parent = str(parent_run_id or "").strip()
        rows = _navigation_rows(value, parent_run_id=parent)
        with self._lock:
            previous = self._rows_by_parent.get(parent, ())
            if previous == rows:
                return False
            self._rows_by_parent[parent] = rows
            for row in rows:
                self._rows_by_run[str(row["run_id"])] = row
            selected = self._selected_by_parent.get(parent, "")
            if selected and selected not in self._selection_ids_locked(parent):
                self._selected_by_parent.pop(parent, None)
            runtime = self._runtimes.get(parent) if parent else self.root_runtime
        if runtime is not None:
            runtime.store.invalidate()
        return True

    # LLM: Goal rows are exact read-only projections, scoped to the root or the
    # current child view. Goal IDs cannot stand in for agent run IDs or authorize control.
    # 函数用途: 刷新当前主子代理视角底部 Goal 行，让方向键和编辑器指向本代理的准确目标。
    def update_goal_rows(self, value: object, *, parent_run_id: str = "") -> bool:
        rows = _goal_navigation_rows(value)
        next_rows = {
            _goal_navigation_id(str(row["goal_id"])): row for row in rows
        }
        with self._lock:
            if parent_run_id:
                if self._child_goal_rows.get(parent_run_id) == next_rows:
                    return False
                self._child_goal_rows[parent_run_id] = next_rows
                self._runtime_for_locked(parent_run_id).store.invalidate()
                return True
            if self._goal_rows == next_rows:
                return False
            self._goal_rows = next_rows
            selected = self._selected_by_parent.get("", "")
            if selected and selected not in self._selection_ids_locked(""):
                self._selected_by_parent.pop("", None)
            if self._expanded_goal_id and self._expanded_goal_id not in next_rows:
                self._expanded_goal_id = ""
            runtime = self.root_runtime
        runtime.store.invalidate()
        return True

    # LLM: Return a copy of the exact current-page selection; it is display data, never control authorization.
    # 函数用途: 编辑器取得被方向键选中的目标，Gateway 保存时仍会重新校验归属和版本。
    def selected_goal(self) -> dict[str, object] | None:
        with self._lock:
            parent = self._active_run_id_locked()
            rows = self._child_goal_rows.get(parent, {}) if parent else self._goal_rows
            row = rows.get(self._selected_by_parent.get(parent, ""))
            return dict(row) if row is not None else None

    # LLM: 方向键仅在输入控制器调用时改变显示选择，进程页仅切日志；不消费历史或发执行控制。
    # 函数用途: 移动选择；有后台入口时上下键都先选它，观察页内部只切日志。
    def move_selection(self, delta: int) -> bool:
        step = int(delta or 0)
        if not step:
            return False
        with self._lock:
            watching = bool(self._path and self._path[-1] == "bg:watch")
            listing = self._business_listing_locked()
            parent = self._path[-1] if listing else "bg:watch" if watching else self._active_run_id_locked()
            ids = self._selection_ids_locked(parent)
            if not ids:
                return False
            selected = self._selected_by_parent.get(parent, "")
            if selected not in ids:
                index = 0 if step > 0 or ids[0].startswith(("bg:", 'business:')) else len(ids) - 1
            else:
                index = max(0, min(len(ids) - 1, ids.index(selected) + step))
            self._selected_by_parent[parent] = ids[index]
            if not parent and self._expanded_goal_id != ids[index]:
                self._expanded_goal_id = ""
            runtime = self._active_runtime_locked()
            if watching:
                self._publish_watch_locked()
            if listing:
                self._publish_business_locked()
        runtime.store.invalidate()
        return True

    # LLM: Enter 只接受已登记的准确选择，bg:watch 始终走独立展示分支，不把输入正文或进程 ID 当 run_id。
    # 函数用途: 展开 Goal、只读后台观察或子代理；后台页不请求代理详情。
    def enter_selected(self) -> bool:
        with self._lock:
            if self._path and self._path[-1].startswith('business:') and not self._business_listing_locked():
                return False
        if self._enter_business_selected():
            return True
        with self._lock:
            if self._path and self._path[-1] == "bg:watch":
                self._publish_watch_locked()
                return True
            parent = self._active_run_id_locked()
            selected = self._selected_by_parent.get(parent, "")
            if not selected or selected not in self._selection_ids_locked(parent):
                return False
            goals = self._child_goal_rows.get(parent, {}) if parent else self._goal_rows
            if selected == "bg:watch":
                self._path.append(selected)
                self._publish_watch_locked()
                runtime, callback = self._active_runtime_locked(), self._view_change
                has_snapshot, lifecycle_phase = True, ""
            elif selected in goals:
                self._expanded_goal_id = "" if self._expanded_goal_id == selected else selected
                runtime = self._active_runtime_locked()
                callback = None
                has_snapshot = True
                lifecycle_phase = ""
            else:
                self._expanded_goal_id = ""
                self._path.append(selected)
                runtime = self._runtime_for_locked(selected)
                callback = self._view_change
                has_snapshot = selected in self._goal_published
                lifecycle_phase = str(
                    self._rows_by_run.get(selected, {}).get("lifecycle_phase") or ""
                )
        if not has_snapshot:
            runtime.set_notice(
                _agent_startup_notice(lifecycle_phase),
                duration_seconds=2.5,
                notice_kind=_AGENT_STARTUP_NOTICE_KIND,
            )
        if callback is not None:
            callback(runtime)
        runtime.store.invalidate()
        return True

    # LLM: Back pops exactly one visual parent. It does not cancel, pause, resume,
    # or mutate either agent, which is why Esc is deliberately not routed here.
    # Before showing the parent, its cached roster is reconciled only with newer
    # typed rows already observed on the descendant page.
    # 函数用途: 用 Ctrl+G、Alt+左箭头或 `/back` 返回上一层，并先修正已知的过期子代理展示状态。
    def back(self) -> bool:
        with self._lock:
            business = bool(self._path and self._path[-1].startswith('business:'))
            if business:
                self._path.pop()
                if self._business_listing_locked():
                    self._publish_business_locked()
                runtime, callback = self._active_runtime_locked(), self._view_change
        if business:
            if callback is not None:
                callback(runtime)
            runtime.store.invalidate()
            return True
        reconcile_rows: tuple[dict[str, object], ...] | None = None
        with self._lock:
            if self._expanded_goal_id:
                self._expanded_goal_id = ""
                runtime = self._active_runtime_locked()
                callback = None
            elif not self._path:
                return False
            else:
                exited_page = self._path.pop()
                parent = self._active_run_id_locked()
                # 返回主输入时结束观察入口的高亮，避免空输入上键继续被旧选择截走。
                if exited_page == "bg:watch" and self._selected_by_parent.get(parent) == exited_page:
                    self._selected_by_parent.pop(parent, None)
                reconcile_rows = _reconcile_parent_rows(
                    self._rows_by_parent.get(parent, ()),
                    self._rows_by_run,
                )
                self._rows_by_parent[parent] = reconcile_rows
                runtime = self._active_runtime_locked()
                callback = self._view_change
        if reconcile_rows is not None:
            reconciler = getattr(runtime, "reconcile_background_subagents", None)
            if callable(reconciler):
                reconciler(reconcile_rows)
        if callback is not None:
            callback(runtime)
        runtime.store.invalidate()
        return True

    # LLM: Typing clears only the current visual selection; the focused child
    # view and every canonical run remain unchanged.
    # 函数用途: 用户开始输入正文时取消方向键高亮，避免误以为 Enter 会继续进入下级。
    def clear_selection(self) -> bool:
        with self._lock:
            parent = self._path[-1] if self._business_listing_locked() else self._active_run_id_locked()
            if parent not in self._selected_by_parent:
                return False
            self._selected_by_parent.pop(parent, None)
            if not parent:
                self._expanded_goal_id = ""
            runtime = self._active_runtime_locked()
        runtime.store.invalidate()
        return True

    # LLM: 快照只投影后端终态和独立的完整活动计数；展示行截断不能改变后台总数。
    # 函数用途: 返回当前代理或只读后台页面的显示状态，不把进程 ID 当 run_id。
    def snapshot(self) -> TuiAgentNavigationSnapshot:
        with self._lock:
            active = self._active_run_id_locked()
            row = dict(self._rows_by_run.get(active, {})) if active else {}
            status = str(row.get("status") or "").strip().upper()
            watching = bool(self._path and self._path[-1] == "bg:watch")
            page = self._path[-1] if self._path else ''
            listing = self._business_listing_locked()
            target = self._business_targets.get(page, {})
            thread_id = target.get('thread_id', '')
            if thread_id:
                row = target
            space_id = page.removeprefix('business:space:') if page.startswith('business:space:') else str(target.get('space_id') or '')
            return TuiAgentNavigationSnapshot(
                active_run_id=active,
                active_name=str(row.get("name") or row.get("role") or active or "main"),
                active_status=status,
                selected_run_id=self._selected_by_parent.get(page if listing else "bg:watch" if watching else active, ""),
                expanded_goal_id=self._expanded_goal_id,
                depth=len(self._path),
                terminal=bool(active and status in _TERMINAL_AGENT_STATUSES),
                active_replaced_by_run_id=str(row.get("replaced_by_run_id") or "").strip(),
                active_failure_label=str(row.get("failure_label") or "").strip(),
                background_process_count=self._background_count,
                background_watch_active=watching,
                can_send_input=not watching and not listing and not (thread_id and self.business_agents and self.business_agents.read_denied),
                active_thread_id=thread_id,
                business_list_active=listing,
                business_agents_enabled=self.business_agents is not None,
                background_agent_count=self.business_agents.count if self.business_agents else None,
                background_agent_running_count=self.business_agents.running_count if self.business_agents else None,
                business_space_id=space_id,
                active_agent_id=target.get('agent_id', ''),
                active_identity_state=str(target.get('identity_state') or ''),
                active_work_state=str(target.get('work_state') or ''),
                route_enabled=self.route_enabled,
            )

    # LLM: The active runtime is a display store selected by the view stack; it
    # cannot be used as a task identity or control capability.
    # 函数用途: 返回当前页面应渲染和显示提示的 TuiRuntime。
    def active_runtime(self) -> TuiRuntime:
        with self._lock:
            return self._active_runtime_locked()

    # LLM: Event cursors are per exact run so switching between children never
    # skips or duplicates another child's process stream.
    # 函数用途: 返回指定子代理已经消费的过程事件游标。
    def event_cursor(self, run_id: str) -> int:
        with self._lock:
            return max(0, int(self._event_cursors.get(str(run_id or ""), 0)))

    # LLM: One authenticated detail payload updates only its exact run runtime.
    # The first non-empty delegated goal becomes the child page's user block;
    # a transient incomplete payload must not permanently suppress that prompt.
    # 函数用途: 把子代理消息快照应用到对应页面；正式回复用 canonical thread/message ID，发布后才确认。
    def apply_agent_view(self, run_id: str, payload: object) -> bool:
        view = _normalize_agent_view_projection(run_id, payload)
        if view is None:
            return False
        runtime, first_goal, prior_final_key, typed_final_already_visible = (
            self._register_agent_view(view)
        )
        if view.payload.get("goal_projection_ok") is not False:
            self.update_goal_rows(view.payload.get("goals") or [], parent_run_id=view.run_id)
        goal = str(view.row.get("goal") or "").strip()
        if first_goal and goal:
            runtime.enqueue_prompt(f"agent-goal:{view.run_id}", goal, queued=False)
            with self._lock:
                self._goal_published.add(view.run_id)
        with self._lock:
            goal_published = view.run_id in self._goal_published
        if isinstance(view.events, list | tuple):
            runtime.publish_background_transcript_events(view.events)
        if view.terminal or goal_published or bool(view.events) or view.final_response:
            runtime.clear_notice(expected_kind=_AGENT_STARTUP_NOTICE_KIND)
        else:
            # 轮询会续上这个短 notice；一旦 goal/首事件到达立即清除。这样既不新增
            # 持久状态，也不会因为首个不完整快照把详情页变成无解释白屏。
            runtime.set_notice(
                _agent_startup_notice(view.lifecycle_phase),
                duration_seconds=2.5,
                notice_kind=_AGENT_STARTUP_NOTICE_KIND,
            )
        if view.terminal:
            runtime.settle_agent_transcript(view.run_id, status=view.status)
        if (
            view.final_response
            and view.final_response_key != prior_final_key
            and not typed_final_already_visible
        ):
            runtime.publish_background_response(
                view.final_response,
                thread_id=str(view.payload.get("thread_id") or ""),
                message_id=view.final_response_key,
            )
        runtime.update_background_activity(
            0 if view.terminal else 1,
            _agent_view_background_activity(view),
        )
        with self._lock:
            if view.final_response_key:
                self._final_response_keys[view.run_id] = view.final_response_key
            self._event_cursors[view.run_id] = max(
                self._event_cursors.get(view.run_id, 0),
                _safe_int(view.payload.get("event_cursor")),
            )
        runtime.store.invalidate()
        return True

    # LLM: Registry mutation is kept under one lock so child rows, selection,
    # typed final identity, and display runtime become visible as one UI projection.
    # Canonical final acknowledgement happens only after publication, not during registration.
    # 函数用途: 原子登记一次已校验的子代理详情，并返回渲染阶段所需的去重状态。
    def _register_agent_view(
        self,
        view: _AgentViewProjection,
    ) -> tuple[TuiRuntime, bool, str, bool]:
        with self._lock:
            self._rows_by_run[view.run_id] = view.row
            self._rows_by_parent[view.run_id] = view.children
            for child in view.children:
                self._rows_by_run[str(child["run_id"])] = child
            child_ids = set(self._selection_ids_locked(view.run_id))
            if self._selected_by_parent.get(view.run_id, "") not in child_ids:
                self._selected_by_parent.pop(view.run_id, None)
            runtime = self._runtime_for_locked(view.run_id)
            first_goal = view.run_id not in self._goal_published
            prior_final_key = self._final_response_keys.get(view.run_id, "")
            self._typed_final_response_request_ids.update(view.typed_final_request_ids)
            typed_final_already_visible = bool(
                view.final_response_request_id
                and view.final_response_request_id
                in self._typed_final_response_request_ids
            )
        return runtime, first_goal, prior_final_key, typed_final_already_visible

    # LLM: 活动代理身份仅来自视图栈；后台观察页必须返回空值，防止误请求代理控制接口。
    # 函数用途: 返回当前真实代理身份；后台观察页没有代理控制目标。
    def _active_run_id_locked(self) -> str:
        return self._path[-1] if self._path and self._path[-1] != "bg:watch" and not self._path[-1].startswith('business:') else ""

    # LLM: 根页面按后台入口、Goal、直属子代理排序；后台页只有进程选择，这一顺序不改变生命周期。
    # 函数用途: 后台入口优先于 Goal/子代理，后台页内部只选择各进程。
    def _selection_ids_locked(self, parent_run_id: str) -> list[str]:
        parent = str(parent_run_id or "").strip()
        if parent == "bg:watch":
            return [f"bg:{row['session_id']}" for row in self._background_rows]
        if parent == 'business:agents' or parent.startswith('business:space:'):
            space_id = parent.removeprefix('business:space:') if parent.startswith('business:space:') else None
            return [row['navigation_id'] for row in self.business_agents.navigation_rows(space_id)] if self.business_agents else []
        ids = list(self._goal_rows) if not parent else list(self._child_goal_rows.get(parent, {}))
        if not parent and self.business_agents is not None:
            ids.insert(0, 'business:agents')
        if not parent and self._background_count:
            ids.insert(0, "bg:watch")
        ids.extend(
            str(row["run_id"])
            for row in self._rows_by_parent.get(parent, ())
            if str(row.get("run_id") or "").strip()
        )
        return ids

    # LLM: Runtime allocation uses one session-local display namespace per run;
    # no Agent, worker, queue, or model backend is constructed.
    # 函数用途: 创建或取得一个子代理详情页的 typed 展示 runtime。
    def _runtime_for_locked(self, run_id: str) -> TuiRuntime:
        runtime = self._runtimes.pop(run_id, None)
        if runtime is None:
            from .tui_view_model import TuiStateStore, TuiViewModelReducer

            # 子代理详情页沿用主页的展示窗口，不另设上限。
            window = self.root_runtime.store.reducer.max_stable_blocks
            runtime = TuiRuntime(f"{self.root_runtime.session_id}:agent:{run_id}",
                                 store=TuiStateStore(reducer=TuiViewModelReducer(max_stable_blocks=window)))
        self._runtimes[run_id] = runtime
        self._evict_runtimes_locked(keep=run_id)
        return runtime

    # LLM: 路径、业务会话和本次返回页始终保留；回收只清展示状态，重进后沿原事件重放。
    # 函数用途: 按最久未访问顺序回收超额 runtime 及其游标和去重标记，保留代理名册。
    def _evict_runtimes_locked(self, *, keep: str) -> None:
        if len(self._runtimes) <= self._max_cached_runtimes:
            return
        pinned = set(self._path) | {keep}
        for run_id in list(self._runtimes):
            if len(self._runtimes) <= self._max_cached_runtimes:
                break
            if run_id in pinned:
                continue
            self._runtimes.pop(run_id, None)
            target = self._business_targets.pop(run_id, None)
            if target is not None:
                thread_id = target['thread_id']
                self._business_messages.pop(thread_id, None)
                self._business_history.pop(thread_id, None)
                self._business_tools.pop(thread_id, None)
            self._event_cursors.pop(run_id, None)
            self._final_response_keys.pop(run_id, None)
            self._goal_published.discard(run_id)

    # LLM: 展示 runtime 按真实栈顶查找，保留独立后台页；不能复用空代理控制身份来决定页面。
    # 函数用途: 返回当前页面展示 runtime，后台页使用独立且无执行权的页面。
    def _active_runtime_locked(self) -> TuiRuntime:
        active = self._path[-1] if self._path else ""
        return self._runtime_for_locked(active) if active else self.root_runtime


# LLM: Descendant detail polling may hold a newer typed row than the parent's
# cached roster. Merge only rows whose structured updated_at is at least as new;
# no status is inferred or made monotonic.
# 函数用途: 返回上一层时把刚查看代理的最新状态合回父列表，避免旧缓存短暂把已停止代理画成运行中。
def _reconcile_parent_rows(
    cached: tuple[dict[str, object], ...],
    rows_by_run: Mapping[str, dict[str, object]],
) -> tuple[dict[str, object], ...]:
    reconciled: list[dict[str, object]] = []
    for row in cached:
        run_id = str(row.get("run_id") or "").strip()
        latest = rows_by_run.get(run_id)
        if latest is None or _row_updated_at(latest) < _row_updated_at(row):
            reconciled.append(row)
        else:
            reconciled.append(dict(latest))
    return tuple(reconciled)


# LLM: Gateway detail payloads cross an untrusted presentation boundary. Normalize
# exact run identity and bounded row/event facts once before mutating navigation state.
# A final without its canonical thread/message id is not guessed from prose or a legacy alias.
# 函数用途: 校验并整理一份子代理详情响应，非法或串代理的数据直接拒绝。
def _normalize_agent_view_projection(
    run_id: str,
    payload: object,
) -> _AgentViewProjection | None:
    selected = str(run_id or "").strip()
    if not selected or not isinstance(payload, Mapping) or payload.get("ok") is not True:
        return None
    raw_agent = payload.get("agent")
    if not isinstance(raw_agent, Mapping):
        return None
    row = _navigation_row(raw_agent, expected_run_id=selected)
    if row is None:
        return None
    children = _navigation_rows(payload.get("children"), parent_run_id=selected)
    events = payload.get("transcript_events")
    final_response = str(payload.get("final_response") or "").strip()
    final_response_request_id = _final_response_request_id(
        payload.get("final_response_request_id"),
        selected,
    )
    final_response_key = str(payload.get("final_response_message_id") or "").strip()
    if not final_response_key or not str(payload.get("thread_id") or "").strip():
        final_response = ""
    return _AgentViewProjection(
        run_id=selected,
        payload=payload,
        raw_agent=raw_agent,
        row=row,
        children=children,
        events=events,
        typed_final_request_ids=frozenset(
            _typed_final_response_request_ids(events, selected)
        ),
        final_response=final_response,
        final_response_request_id=final_response_request_id,
        final_response_key=final_response_key,
        terminal=bool(payload.get("terminal")),
        status=str(row.get("status") or "").strip().upper(),
        lifecycle_phase=str(row.get("lifecycle_phase") or "").strip().lower(),
    )


# LLM: Activity rendering derives only from the normalized child projection. Keep
# this payload compatible with TuiRuntime and never infer lifecycle from prose.
# 函数用途: 生成子代理详情页底部 Working、上下文、Todo 和下级代理的展示数据。
def _agent_view_background_activity(view: _AgentViewProjection) -> dict[str, object]:
    row = view.row
    payload = view.payload
    main_activity = {
        "task_id": view.run_id,
        "phase": _agent_activity_phase(
            view.status,
            view.terminal,
            lifecycle_phase=view.lifecycle_phase,
        ),
        "activity": str(row.get("activity") or "").strip(),
        "started_at": row.get("created_at", 0.0),
        "updated_at": row.get("updated_at", 0.0),
        "ended_at": row.get("ended_at", 0.0),
        **(
            {"context_usage": dict(view.raw_agent["context_usage"])}
            if isinstance(view.raw_agent.get("context_usage"), Mapping)
            else {}
        ),
    }
    return {
        "compact_count": max(0, _safe_int(row.get("compact_count"))),
        "model_metrics": view.raw_agent.get("model_metrics"),
        "main_activity": main_activity,
        "subagents": view.children,
        "goals": payload.get("goals"),
        "goal_projection_ok": payload.get("goal_projection_ok") is not False,
        "task_progress": {
            "items": payload.get("task_progress_items"),
            "generation_id": payload.get("task_progress_generation_id"),
            "plan_revision": payload.get("task_progress_plan_revision"),
        },
        "hidden_subagent_count": max(
            0,
            _safe_int(payload.get("hidden_child_count")),
        ),
        "projection_ok": True,
        "task_progress_projection_ok": isinstance(
            payload.get("task_progress_items"), list | tuple
        ),
    }


# LLM: Row normalization allows only bounded scalar display fields plus exact
# progress ids. Nested context usage remains outside row selection state.
# 函数用途: 清洗一个代理导航行，并校验可选的精确 run id。
def _navigation_row(
    value: object,
    *,
    expected_run_id: str = "",
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    run_id = str(value.get("run_id") or "").strip()
    if not run_id or (expected_run_id and run_id != expected_run_id):
        return None
    row = {
        str(key): value[key]
        for key in _ROW_SCALAR_FIELDS
        if key in value and not isinstance(value[key], dict | list | tuple | set)
    }
    row["run_id"] = run_id
    progress_ids = value.get("progress_item_ids")
    if isinstance(progress_ids, list | tuple):
        row["progress_item_ids"] = [
            str(item).strip()[:128]
            for item in progress_ids[:24]
            if str(item or "").strip()
        ]
    return row


# LLM: A row page must contain exact children of the supplied parent. Malformed
# or cross-parent rows are dropped rather than guessed into the visible tree.
# 函数用途: 清洗某一层的直属子代理列表。
def _navigation_rows(
    value: object,
    *,
    parent_run_id: str,
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    rows: list[dict[str, object]] = []
    for item in value[:64]:
        row = _navigation_row(item)
        if row is None:
            continue
        actual_parent = str(row.get("parent_run_id") or "").strip()
        if parent_run_id and actual_parent != parent_run_id:
            continue
        rows.append(row)
    return tuple(rows)


# LLM: Goal navigation ids are an explicit namespace distinct from run ids, so
# an Enter action can never accidentally call the child-agent endpoint.
# 函数用途: 为底部 Goal 行生成稳定且不会与子代理冲突的选择 ID。
def _goal_navigation_id(goal_id: str) -> str:
    return f"goal:{str(goal_id or '').strip()}"


# LLM: Goal row normalization accepts only bounded public scalars supplied by
# the authenticated conversation activity projection.
# 函数用途: 清洗可供方向键选择和详情展示的 Goal 列表。
def _goal_navigation_rows(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    rows: list[dict[str, object]] = []
    for item in value[:16]:
        if not isinstance(item, Mapping):
            continue
        goal_id = str(item.get("goal_id") or "").strip()
        if not goal_id:
            continue
        row = {
            str(key): item[key]
            for key in _GOAL_ROW_SCALAR_FIELDS
            if key in item and not isinstance(item[key], dict | list | tuple | set)
        }
        row["goal_id"] = goal_id
        rows.append(row)
    return tuple(rows)


# LLM: A typed non-process assistant completion is the canonical visible final
# for one request. Record only exact bg-agent identities from the frozen public
# transcript schema so a later history fallback cannot append the same reply.
# 函数用途: 从本次子代理事件里提取已经显示过的正式最终回复回合标识。
def _typed_final_response_request_ids(value: object, run_id: str) -> set[str]:
    if not isinstance(value, list | tuple):
        return set()
    prefix = f"bg-agent:{str(run_id or '').strip()}:"
    request_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        request_id = str(item.get("request_id") or "").strip()
        block_id = str(item.get("block_id") or "").strip()
        payload = item.get("payload")
        if (
            item.get("schema") != BACKGROUND_TRANSCRIPT_SCHEMA
            or item.get("kind") != "assistant_completed"
            or item.get("phase") != "completed"
            or not request_id.startswith(prefix)
            or not block_id.startswith(f"{request_id}:")
            or not isinstance(payload, Mapping)
            or payload.get("process") is True
            or not str(payload.get("text") or "").strip()
        ):
            continue
        request_ids.add(request_id)
    return request_ids


# LLM: The final-response fallback may join only an exact request identity in
# the current child namespace. Invalid values remain empty; the independent canonical
# message ID still owns history identity, never a key derived from response prose.
# 函数用途: 校验 Gateway 返回的最终回复回合标识确实属于当前子代理。
def _final_response_request_id(value: object, run_id: str) -> str:
    request_id = str(value or "").strip()
    prefix = f"bg-agent:{str(run_id or '').strip()}:"
    if not request_id.startswith(prefix) or len(request_id) > 512:
        return ""
    return request_id


# LLM: Activity phase is a display mapping from canonical status/terminal facts;
# it cannot change the run state or decide success.
# 函数用途: 选择详情页主 Working 行的动画或终态样式。
def _agent_activity_phase(
    status: str,
    terminal: bool,
    *,
    lifecycle_phase: str = "",
) -> str:
    if not terminal:
        phase = str(lifecycle_phase or "").strip().lower()
        return phase if phase in {"queued", "starting", "waiting_first_event"} else "running"
    if status == "DONE":
        return "completed"
    if status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return "failed"
    return "interrupted"


# LLM: Startup copy is selected only from the authenticated lifecycle_phase scalar. It is a
# transient TUI notice and must never infer status from task text, timestamps, or spinner frames.
# 函数用途: 在子代理详情尚无 prompt/事件时，用大白话持续解释当前启动阶段。
def _agent_startup_notice(lifecycle_phase: str) -> str:
    return {
        "queued": "子代理已创建，正在排队…",
        "starting": "子代理正在启动执行器…",
        "waiting_first_event": "子代理已开始，正在等待模型首个响应…",
        "running": "子代理正在运行，正在载入首段详情…",
    }.get(str(lifecycle_phase or "").strip().lower(), "正在载入子代理详情…")


# LLM: Numeric coercion affects display counters/cursors only.
# 函数用途: 将不可信展示数字转换成非负整数。
def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: Row recency arbitration is display-only and uses the canonical scalar
# already supplied by the backend; invalid timestamps are simply oldest.
# 函数用途: 读取子代理展示行的更新时间，用来避免返回父页面时拿旧快照覆盖新状态。
def _row_updated_at(value: Mapping[str, object]) -> float:
    try:
        return max(0.0, float(value.get("updated_at") or 0.0))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["TuiAgentNavigationSnapshot", "TuiAgentNavigationState"]
