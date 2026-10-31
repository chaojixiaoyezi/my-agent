# LLM: 进度投递回执只承诺 provider 确认的消息 ID，不等同最终答复或任务完成；由原 pending 持久化。
# 模块用途: 定义同一条进度更新的结构化发送结果，供统一投递服务与适配器 worker 共用。
from __future__ import annotations

from dataclasses import dataclass, field


# LLM: 消息 ID 只能来自 provider 回执或原 pending；failure_kind 是结构化分类，unknown/布尔失败不证明外部副作用未发生。
# 类用途: 保存进度发送或编辑的实际结果及永久/暂时/未知失败事实，错误不阻止最终回复。
@dataclass(frozen=True)
class ProgressDeliveryReceipt:
    ok: bool
    message_id: str = ""
    error_code: str = ""
    failure_kind: str = "unknown"
    render_kind: str = field(default='text', kw_only=True)
    display_message_id: str = field(default='', kw_only=True)
    source_revision: int = field(default=0, kw_only=True)
    reconcile_pending: bool = field(default=False, kw_only=True)

    # LLM: 兼容 worker 的布尔发送判据，只以明确 ok 为发送成功，不能把非空对象当成功。
    # 函数用途: 在原外发状态机里使用真实发送结果。
    def __bool__(self) -> bool:
        return self.ok


def render_progress_card(snapshot: dict) -> dict:
    """W6 未合入时的无状态兼容渲染；只消费原工具事件，不读写任务或投递状态。"""
    from ..conversation.channels import project_host_paths_for_channel, project_user_reply

    rows = []
    steps = snapshot.get('steps') or ()
    for item in steps[-50:]:
        phase, ok = item.get('phase'), item.get('ok')
        state = ('完成' if ok is True else '失败' if ok is False else '状态未确认') if phase == 'finished' else (
            '执行中' if phase in {'started', 'running'} else '状态未确认')
        label = project_host_paths_for_channel(project_user_reply(item.get('label') or item.get('tool_name') or '').content, 'feishu')[:160]
        rows.append(f'• {label} · {state}')
    detail = '\n'.join(rows) or '等待真实执行事件'
    if snapshot.get('truncated'):
        detail += '\n仅显示最近 50 个步骤；其余已折叠，完整记录见原任务。'
    return {'schema': '2.0', 'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': '执行记录'}},
        'body': {'elements': [{'tag': 'collapsible_panel', 'expanded': False,
            'header': {'title': {'tag': 'plain_text', 'content': '查看执行步骤'}},
            'elements': [{'tag': 'markdown', 'content': detail}]}]}}
