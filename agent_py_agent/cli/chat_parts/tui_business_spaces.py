"""TUI 的业务命令适配；只消费明确 slash，不读取 Owner 业务文件。"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable

from ..space_commands import (
    BusinessCommandSession,
    business_client_for_agent,
    consume_business_command,
)


def handle_tui_business_command(raw: str, *, agent: object, print_line: Callable[[str], None]) -> bool:
    if raw.strip().partition(' ')[0] not in {'/space', '/team', '/workflow'}:
        return False
    session = getattr(agent, '_tui_business_command_session', None)
    if session is None:
        session = BusinessCommandSession(business_client_for_agent(agent))
        agent._tui_business_command_session = session
    # 工作线程按命令消费；同一实例表格编号不被并发 list/get 冲掉。
    lock = getattr(agent, '_tui_business_command_lock', None)
    if lock is None:
        lock = threading.Lock()
        agent._tui_business_command_lock = lock
    try:
        with lock:
            result = consume_business_command(raw, session=session)
        if result and 'display_rows' in result:
            print_line('名称 | 关联空间')
            for row in result['display_rows']:
                print_line(' | '.join(row))
            print_line('覆盖：' + str(result.get('data', {}).get('coverage', 'unknown')))
        else:
            print_line(json.dumps(result, ensure_ascii=False))
    except (ValueError, TypeError) as exc:
        print_line(f'业务命令未发送：{exc}')
    return True
