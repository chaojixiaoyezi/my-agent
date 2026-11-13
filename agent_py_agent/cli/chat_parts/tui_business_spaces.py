"""TUI 的业务命令适配；只消费明确 slash，不读取 Owner 业务文件。"""
from __future__ import annotations

# LLM: TUI 与聊天共用 BusinessCommandSession，保证同一会话内的工作流编号一致；会话当前空间
# 不由客户端保存（没有写入端口），/space use 只展示详情并明确未切换（V02-02 / V07-03 / V13-03）。
# 命令执行过程持有线程锁避免并发渲染交织，不读取底层文件系统（V02-02 / V07-03 / V13-03）。
# 模块用途: 统一适配 /space、/team 与 /workflow 的终端交互命令。
import json
import threading
from collections.abc import Callable

from ..space_commands import (
    BusinessCommandSession,
    business_client_for_agent,
    consume_business_command,
)


# LLM: 拦截并消费以 /space、/team 或 /workflow 开头的显式命令，
# 按照两列格式展示流程列表，或展示格式化详情及标准 JSON 回执。
# 函数用途: 处理 TUI 与命令行交互中的业务空间、团队与流程控制命令。
def handle_tui_business_command(raw: str, *, agent: object, print_line: Callable[[str], None],
                                session_id: str = '') -> bool:
    if raw.strip().partition(' ')[0] not in {'/space', '/team', '/workflow'}:
        return False
    session = getattr(agent, '_tui_business_command_session', None)
    if session is None or getattr(agent, '_tui_business_command_scope', '') != session_id:
        session = BusinessCommandSession(business_client_for_agent(agent, conversation_id=session_id))
        agent._tui_business_command_session = session
        agent._tui_business_command_scope = session_id
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
        elif result and (result.get('data') or {}).get('switch') == 'not_performed':
            # V02-02：让「未切换」在终端可见，不把只读详情渲染成切换成功。
            space = (result.get('data') or {}).get('space') or {}
            print_line(f"空间：{space.get('name', '')}（{space.get('space_id', '')}）")
            print_line(str((result.get('error') or {}).get('message', '')))
        elif result and 'rendered' in result and any(raw.strip().startswith(p) for p in ('/workflow show', '/workflow get')):
            print_line(str(result['rendered']))
        else:
            print_line(json.dumps(result, ensure_ascii=False))
    except (ValueError, TypeError) as exc:
        print_line(f'业务命令未发送：{exc}')
    return True
