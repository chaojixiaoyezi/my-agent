"""后台主 Agent 的只读名册/完整消息消费；不是子代理或进程生命周期实现。"""
from __future__ import annotations

import json
import threading

from ..space_commands import BusinessClient, PreparedBusinessCall, business_client_for_agent


class TuiBackgroundAgents:
    def __init__(self, client: BusinessClient, stop_event: threading.Event | None = None):
        self.client = client
        self.stop_event = stop_event
        self.read_denied = False
        self.rows: tuple[dict, ...] = ()
        self.count: int | None = None
        self.running_count: int | None = None
        self.coverage = 'unknown'

    def refresh(self) -> None:
        page = self.client.call('background_agents.list', {})
        self.read_denied = page.get('http_status') in (401, 403, 404)
        if self.read_denied:
            self.rows = ()
        data = page.get('data') if page.get('ok') is True else None
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            self.count, self.running_count, self.coverage = None, None, 'unknown'
            return
        self.rows = tuple(dict(row) for row in data['items'] if isinstance(row, dict) and row.get('agent_id'))
        self.coverage = data.get('coverage', 'unknown')
        self.count = len(self.rows) if self.coverage == 'complete' and not data.get('next_cursor') else None
        # Agent 身份状态不等于当前执行；没有执行计数事实就明说未知。
        self.running_count = None

    def lines(self, selected: str = '') -> tuple[str, ...]:
        result = [f'后台团队 · 覆盖 {self.coverage} · 运行数未知 · 费用未知',
                  'Enter 完整会话 · /tree 按需展开子树 · /log <引用> 按需读日志 · Ctrl+G 返回']
        for row in self.rows:
            marker = '› ' if selected == 'business:' + row['agent_id'] else '  '
            result.append(marker + ' · '.join(str(row.get(key) or '未知') for key in
                          ('name', 'space_id', 'purpose', 'model_profile_id')) + ' · 当前工作/阻塞：未知')
        if not self.rows:
            result.append('没有可显示的条目' if self.coverage == 'complete' else '未连接或资料不完整，数量未知')
        return tuple(result)

    def conversation(self, agent_id: str) -> dict:
        """按原 message_id 翻页，直到真实末页；坏游标不冒充完整历史。"""
        params = {'target': {'kind': 'background_main', 'id': agent_id}}
        items, message_ids, cursors = [], set(), set()
        coverage = 'complete'
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                return {'items': items, 'coverage': 'partial'}
            result = self.client.call('messages.list', params)
            data = result.get('data') if result.get('ok') is True else None
            if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                return {'items': items, 'coverage': 'unknown'}
            if data.get('coverage') != 'complete':
                coverage = data.get('coverage', 'unknown')
            for row in data['items']:
                if row.get('thread_id') != agent_id or not row.get('message_id'):
                    coverage = 'partial'
                    continue
                if row['message_id'] not in message_ids:
                    message_ids.add(row['message_id'])
                    items.append(dict(row))
            cursor = data.get('next_cursor')
            if not cursor:
                return {'items': items, 'coverage': coverage}
            if cursor in cursors:
                return {'items': items, 'coverage': 'partial'}
            cursors.add(cursor)
            params['cursor'] = cursor

    def load_tree(self, space_id: str, agent_id: str) -> dict:
        return self.client.call('agent_tree.get', {'space_id': space_id, 'root_agent_id': agent_id})

    def load_log(self, source_ref: str) -> dict:
        return self.client.call('logs.read', {'source_ref': source_ref})

    def prepare_input(self, agent_id: str, text: str) -> PreparedBusinessCall:
        return self.client.prepare('background_agents.messages.send',
                                   {'agent_id': agent_id, 'text': text, 'input_kind': 'message'})


def start_business_observation(agent: object, navigation: object, stop_event: threading.Event) -> threading.Thread:
    """只启动 UI 只读观察；退出页面仅停止此观察，不停后台工作。"""
    projection = TuiBackgroundAgents(business_client_for_agent(agent), stop_event)

    def observe():
        while not stop_event.is_set():
            try:
                projection.refresh()
                navigation.update_business_agents(projection)
            except Exception:  # 展示失败必须显性，不能终止 TUI 或伪造空名册。
                projection.count, projection.running_count, projection.coverage = None, None, 'unknown'
                navigation.update_business_agents(projection)
            stop_event.wait(5)

    thread = threading.Thread(target=observe, name='tui-business-observation', daemon=True)
    thread.start()
    return thread


def submit_business_input(event, params, text: str, *, has_media: bool = False) -> bool:
    """UI 线程先冻结准确目标与请求，再异步投递；切回父页不会改写该目标。"""
    navigation = getattr(params, 'agent_navigation', None)
    snapshot = navigation.snapshot() if navigation else None
    thread_id = getattr(snapshot, 'active_thread_id', '')
    if not thread_id:
        return False
    runtime = navigation.active_runtime()
    projection = navigation.business_agents
    if has_media:
        runtime.set_notice('后台会话附件请通过业务媒体入口上传；草稿保留。', duration_seconds=3)
        return True
    if text.startswith('/') and text != '/tree' and not text.startswith('/log '):
        runtime.set_notice('当前为后台会话；Ctrl+G 返回后使用 /team 等命令，未发送。', duration_seconds=3)
        return True
    prepared = None if text.startswith('/') else projection.prepare_input(thread_id, text)
    row = next((row for row in projection.rows if row['agent_id'] == thread_id), {})
    space_id = row.get('space_id')

    async def send():
        import asyncio

        if text == '/tree':
            result = await asyncio.to_thread(projection.load_tree, space_id, thread_id)
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
