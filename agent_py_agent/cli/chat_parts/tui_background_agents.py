"""后台主 Agent 的只读名册/完整消息消费；不是子代理或进程生命周期实现。"""
from __future__ import annotations

import json
import threading

from ...agent.plugin_host.errors import HostAPIError
from ..space_commands import BusinessClient, PreparedBusinessCall, business_client_for_agent
from .tui_threading import TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS


class TuiBackgroundAgents:
    # LLM: 由TUI业务观察入口注入客户端和可选停止信号；只初始化本地未知计数、分页缓存与锁，不请求后端或启动观察线程。
    # 函数用途: 建立后台团队的界面投影，初始未知数量不冒充零。
    def __init__(self, client: BusinessClient, stop_event: threading.Event | None = None):
        self.client = client
        self.stop_event = stop_event
        self.read_denied = False
        self.rows: tuple[dict, ...] = ()
        self.count: int | None = None
        self.running_count: int | None = None
        self.coverage = 'unknown'
        self._pages: dict[str, dict] = {}
        self._lock = threading.RLock()

    # LLM: 底栏只读一个有界页；Owner总量取完整配额事实，名册分页不影响它，运行数仍要求完整工作事实。
    # 函数用途: 分别更新身份总量与实际运行数，配额未知不退回局部页数补零。
    def refresh(self) -> None:
        page = self.client.call('background_agents.list', {'limit': 50})
        self.read_denied = page.get('http_status') in (401, 403, 404)
        if self.read_denied:
            with self._lock:
                self.rows = ()
                self._pages.clear()
        data = page.get('data') if page.get('ok') is True else None
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            self.count, self.running_count, self.coverage = None, None, 'unknown'
            return
        self.rows = tuple(dict(row) for row in data['items'][:50]
                          if isinstance(row, dict) and isinstance(row.get('agent_id'), str) and row['agent_id'])
        self.coverage = data.get('coverage', 'unknown')
        complete = (self.coverage == 'complete' and not data.get('next_cursor') and len(self.rows) <= 50
                    and len(self.rows) == len(data['items']) and len({row['agent_id'] for row in self.rows}) == len(self.rows))
        if 'quota' in data:
            quota = data['quota']
            used = quota.get('used') if isinstance(quota, dict) else None
            self.count = used if (isinstance(quota, dict) and quota.get('coverage') == 'complete'
                                  and type(used) is int and used >= 0) else None
        else:
            self.count = len(self.rows) if complete else None
        known_work = {'idle', 'running', 'waiting', 'queued', 'failed', 'completed', 'cancelled'}
        running = sum(row.get('work_state') == 'running' for row in self.rows)
        self.running_count = running if (complete and self.count is not None and running <= self.count <= len(self.rows)
            and all(isinstance(row.get('work_state'), str) and row['work_state'] in known_work for row in self.rows)) else None
        # 只合并本页已经观察到的准确身份，不能用第一页缺席推断其它空间内Agent消失。
        latest = {row['agent_id']: row for row in self.rows}
        with self._lock:
            for key, cached in self._pages.items():
                if key == 'spaces':
                    continue
                cached['items'] = tuple(dict(latest[row['agent_id']]) if row['agent_id'] in latest
                    and latest[row['agent_id']].get('space_id') == row.get('space_id') else row
                    for row in cached.get('items', ()))

    # LLM: 空间和无归属各取一页，不读空间内Agent，更不读取消息历史。
    # 函数用途: 按需展开混合根层，保留真实分页续点。
    def load_root(self) -> None:
        self._read_page('spaces', 'spaces.list', {})
        if not self.read_denied:
            self._read_page('unassigned', 'background_agents.list', {'space_id': None})

    # LLM: 空间身份仅取已观察节点，后端仍复核Owner与空间授权。
    # 函数用途: 只读取指定空间的一个Agent页。
    def load_space(self, space_id: str) -> None:
        self._read_page('space:' + space_id, 'background_agents.list', {'space_id': space_id})

    # LLM: 读页与投影分开，重复游标只报告partial，不发自动重试。
    # 函数用途: 更新一个有界空间或名册窗口并校验准确归属。
    def _read_page(self, key: str, action: str, params: dict, *, append: bool = False) -> None:
        with self._lock:
            previous = self._pages.get(key, {}) if append else {}
            cursor = previous.get('next_cursor')
            if append and not cursor:
                return
            request = {**params, 'limit': 50}
            if cursor:
                request['cursor'] = cursor
            try:
                response = self.client.call(action, request)
            except (HostAPIError, OSError, ValueError, TypeError):
                self._pages[key] = {'items': (), 'coverage': 'unknown', 'next_cursor': None}
                return
            if response.get('http_status') in (401, 403, 404):
                self.read_denied = True
                self._pages.clear()
                self.rows = ()
                self.count = self.running_count = None
                self.coverage = 'unknown'
                return
            data = response.get('data') if response.get('ok') is True else None
            if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                self._pages[key] = {'items': (), 'coverage': 'unknown', 'next_cursor': None}
                return
            identifier = 'space_id' if action == 'spaces.list' else 'agent_id'
            items = {row[identifier]: dict(row) for row in previous.get('items', ())}
            coverage = data.get('coverage', 'unknown')
            if coverage not in ('complete', 'partial', 'unknown') or len(data['items']) > 50:
                coverage = 'partial'
            for row in data['items'][:50]:
                if not isinstance(row, dict) or not isinstance(row.get(identifier), str) or not row[identifier]:
                    coverage = 'partial'
                    continue
                if action != 'spaces.list' and ('space_id' not in row or row['space_id'] != params['space_id']):
                    coverage = 'partial'
                    continue
                items[row[identifier]] = dict(row)
            seen_cursors = set(previous.get('cursors', ()))
            if cursor:
                seen_cursors.add(cursor)
            next_cursor = data.get('next_cursor')
            if next_cursor is not None and not isinstance(next_cursor, str):
                coverage, next_cursor = 'partial', None
            if next_cursor in seen_cursors:
                coverage, next_cursor = 'partial', None
            self._pages[key] = {'items': tuple(items.values()), 'coverage': coverage,
                                'next_cursor': next_cursor, 'cursors': seen_cursors}

    # LLM: 只有用户选中续页节点才推进一页，spaces与unassigned续点互不替代。
    # 函数用途: 读取当前层某一类节点的下一页。
    def load_more(self, space_id: str | None, kind: str) -> None:
        if space_id is not None:
            self._read_page('space:' + space_id, 'background_agents.list', {'space_id': space_id}, append=True)
        elif kind == 'spaces':
            self._read_page('spaces', 'spaces.list', {}, append=True)
        elif kind == 'unassigned':
            self._read_page('unassigned', 'background_agents.list', {'space_id': None}, append=True)

    # LLM: 根有两个独立覆盖面；任一unknown或未读都不能声称空空间/完整名册。
    # 函数用途: 提供当前层真实覆盖率。
    def list_coverage(self, space_id: str | None) -> str:
        with self._lock:
            keys = ('space:' + space_id,) if space_id is not None else ('spaces', 'unassigned')
            pages = [self._pages.get(key, {}) for key in keys]
            if any(page.get('coverage', 'unknown') == 'unknown' for page in pages):
                return 'unknown'
            return 'complete' if all(page.get('coverage') == 'complete' and not page.get('next_cursor') for page in pages) else 'partial'

    # LLM: 只适配两种已有实体和分页控制节点，后台Agent永不伪装成subagent run。
    # 函数用途: 生成当前层稳定导航标识与原始准确目标。
    def navigation_rows(self, space_id: str | None) -> tuple[dict, ...]:
        with self._lock:
            result = []
            if space_id is None:
                for row in self._pages.get('spaces', {}).get('items', ()):
                    result.append({**row, 'kind': 'space', 'navigation_id': 'business:space:' + row['space_id']})
            key = 'unassigned' if space_id is None else 'space:' + space_id
            for row in self._pages.get(key, {}).get('items', ()):
                result.append({**row, 'kind': 'background_main', 'navigation_id': 'business:' + row['agent_id']})
            keys = ('spaces', 'unassigned') if space_id is None else (key,)
            for page_key in keys:
                if self._pages.get(page_key, {}).get('next_cursor'):
                    result.append({'kind': 'page', 'page_kind': page_key,
                                   'navigation_id': 'business:more:' + page_key, 'name': '加载下一页'})
            return tuple(result)

    # LLM: 身份状态和当前工作独立本地化；缺少work_state时不把available写成待命。
    # 函数用途: 沿原列表渲染显示空间、后台Agent及有界分页入口。
    def lines(self, selected: str = '', *, space_id: str | None = None) -> tuple[str, ...]:
        coverage = self.list_coverage(space_id)
        result = [f'后台团队 · 覆盖 {coverage} · 费用未知',
                  'Enter 完整会话 · /tree 按需展开子树 · /log <引用> 按需读日志 · Ctrl+G 返回']
        rows = self.navigation_rows(space_id)
        for row in rows:
            marker = '› ' if selected == row['navigation_id'] else '  '
            if row['kind'] != 'background_main':
                result.append(marker + ('业务空间 · ' if row['kind'] == 'space' else '') + str(row.get('name', '未知')))
                continue
            identity = {'available': '可接单', 'suspended': '已挂起', 'retiring': '退役中', 'retired': '已退役'}.get(str(row.get('identity_state') or ''), '未知')
            work = {'idle': '待命（无进行中任务）', 'running': '运行中', 'waiting': '等待', 'queued': '排队',
                    'failed': '失败', 'completed': '已完成', 'cancelled': '已取消'}.get(str(row.get('work_state') or ''), '未知')
            result.append(marker + str(row.get('name') or row['agent_id']) + ' · ' + str(row.get('space_id') or '无归属') +
                          f' · 身份：{identity} · 工作：{work} · ' + str(row.get('purpose') or '工作摘要未知'))
        if not rows:
            result.append('没有可显示的条目' if coverage == 'complete' else '未连接或资料不完整，数量未知')
        return tuple(result)

    # LLM: 每次只读一个有界消息页；业务target是agent_id，内容归属按独立thread_id校验。
    # 函数用途: 保留完整消息正文及原message_id，续页由用户显式请求。
    def conversation(self, agent_id: str, *, thread_id: str = '', cursor: str | None = None) -> dict:
        params = {'target': {'kind': 'background_main', 'id': agent_id}, 'limit': 100}
        if cursor:
            params['cursor'] = cursor
        if self.stop_event is not None and self.stop_event.is_set():
            return {'items': [], 'coverage': 'partial', 'next_cursor': cursor}
        result = self.client.call('messages.list', params)
        if result.get('http_status') in (401, 403, 404):
            with self._lock:
                self.read_denied = True
                self.rows = ()
                self._pages.clear()
                self.count = self.running_count = None
                self.coverage = 'unknown'
        data = result.get('data') if result.get('ok') is True else None
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            return {'items': [], 'coverage': 'unknown', 'next_cursor': cursor,
                    'read_denied': result.get('http_status') in (401, 403, 404)}
        coverage = data.get('coverage', 'unknown')
        items, message_ids = [], set()
        for row in data['items'][:100]:
            if not isinstance(row, dict) or row.get('thread_id') != (thread_id or agent_id) or not row.get('message_id'):
                coverage = 'partial'
                continue
            if row['message_id'] not in message_ids:
                message_ids.add(row['message_id'])
                items.append(dict(row))
        next_cursor = data.get('next_cursor')
        if next_cursor or len(data['items']) > 100:
            coverage = 'partial'
        if cursor and cursor == next_cursor:
            next_cursor = None
        return {'items': items, 'coverage': coverage, 'next_cursor': next_cursor}

    # LLM: 仅在用户显式请求树时由TUI输入处理调用；通过原客户端读取指定空间/无归属中的准确根Agent，不派活或控制子代理生命周期。
    # 函数用途: 按需读取一个后台助手的组织树。
    def load_tree(self, space_id: str | None, agent_id: str) -> dict:
        return self.client.call('agent_tree.get', {'space_id': space_id, 'root_agent_id': agent_id})

    def load_log(self, source_ref: str) -> dict:
        return self.client.call('logs.read', {'source_ref': source_ref})

    def prepare_input(self, agent_id: str, text: str) -> PreparedBusinessCall:
        return self.client.prepare('background_agents.messages.send',
                                   {'agent_id': agent_id, 'text': text, 'input_kind': 'message'})


# LLM: route只读共享设置，刷新频率有界且不创建线程；退出仅停止观察而非业务工作。
# 函数用途: 同步TUI底栏的会话模式，不在每帧渲染时做磁盘I/O。
def start_route_observation(agent: object, navigation: object, stop_event: threading.Event) -> threading.Thread:
    from .control_runtime import chat_route_setting

    # LLM: 通过原通道解析现有绑定，/new后不沿旧canonical thread读取。
    # 函数用途: 每五秒更新一次只读route显示快照。
    def observe():
        while not stop_event.is_set():
            navigation.update_route_setting(chat_route_setting(agent, navigation.root_runtime.session_id))
            stop_event.wait(5)

    thread = threading.Thread(target=observe, name='tui-route-observation', daemon=True)
    thread.start()
    return thread


def start_business_observation(agent: object, navigation: object, stop_event: threading.Event) -> threading.Thread:
    """只启动 UI 只读观察；退出页面仅停止此观察，不停后台工作。"""
    projection = TuiBackgroundAgents(
        business_client_for_agent(agent, conversation_id=navigation.root_runtime.session_id), stop_event)

    def observe():
        delay, last = 5, None
        while not stop_event.is_set():
            try:
                projection.refresh()
                navigation.update_business_agents(projection)
            except Exception:  # 展示失败必须显性，不能终止 TUI 或伪造空名册。
                projection.count, projection.running_count, projection.coverage = None, None, 'unknown'
                navigation.update_business_agents(projection)
            # 名册投影（数量/运行数/覆盖/拒读/身份与状态）连续不变时按2倍退避，封顶复用TUI通知失败退避上限；一变回5秒。
            current = (projection.count, projection.running_count, projection.coverage, projection.read_denied,
                       tuple((row.get('agent_id'), row.get('work_state')) for row in projection.rows))
            delay = max(5, min(TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS, delay * 2)) if current == last else 5
            last = current
            stop_event.wait(delay)

    thread = threading.Thread(target=observe, name='tui-business-observation', daemon=True)
    thread.start()
    return thread


# LLM: TUI提交入口先在UI线程冻结目标、runtime及预备请求，再安排异步投递；无活动Agent返回False，附件/不支持命令保留草稿，已处理输入才消费并刷新界面。
# 函数用途: 将后台会话输入送给原选定助手，或处理历史、树和日志请求，不随导航切换改投目标。
def submit_business_input(event, params, text: str, *, has_media: bool = False) -> bool:
    """UI 线程先冻结准确目标与请求，再异步投递；切回父页不会改写该目标。"""
    navigation = getattr(params, 'agent_navigation', None)
    snapshot = navigation.snapshot() if navigation else None
    agent_id = getattr(snapshot, 'active_agent_id', '')
    if not agent_id:
        return False
    runtime = navigation.active_runtime()
    projection = navigation.business_agents
    if has_media:
        runtime.set_notice('后台会话附件请通过业务媒体入口上传；草稿保留。', duration_seconds=3)
        return True
    if text == '/history':
        navigation.load_more_business_messages()
        params.input_area.buffer.reset()
        event.app.invalidate()
        return True
    if text.startswith('/') and text != '/tree' and not text.startswith('/log '):
        runtime.set_notice('当前为后台会话；Ctrl+G 返回后使用 /team 等命令，未发送。', duration_seconds=3)
        return True
    prepared = None if text.startswith('/') else projection.prepare_input(agent_id, text)
    space_id = snapshot.business_space_id or None

    # LLM: 由当前app后台任务执行，闭包沿提交时冻结的目标/runtime；线程外调用树、日志或已准备的发送请求，再向原runtime显示回执，不把接收回执当执行完成。
    # 函数用途: 异步完成本次后台会话请求并刷新原会话的显示。
    async def send():
        import asyncio

        if text == '/tree':
            result = await asyncio.to_thread(projection.load_tree, space_id, agent_id)
        elif text.startswith('/log '):
            result = await asyncio.to_thread(projection.load_log, text.partition(' ')[2])
        else:
            result = await asyncio.to_thread(projection.client.send, prepared)
        runtime.write_console(json.dumps(result, ensure_ascii=False))
        runtime.set_notice('回执：' + str(result.get('state', 'unknown')) + '；不等于执行完成', duration_seconds=4)
        event.app.invalidate()

    event.app.create_background_task(send())
    if prepared is not None:
        runtime.write_console('你：' + text)
    params.input_area.buffer.reset()
    event.app.invalidate()
    return True
