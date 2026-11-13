"""结构化结果的简短展示投影；原结果、逻辑引用与归档正文保持完整。"""


from collections.abc import Mapping

from ..common.log_redaction import redact_sensitive_value


def short_tool_invocation(value: str, limit: int = 100) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."



def tool_invocation_summary(tool_name, payload):
    """为前台实时卡和授权后台回放提取同一份有界、脱敏的调用摘要。"""
    if not isinstance(payload, Mapping):
        return ""
    if tool_name == 'read_artifact':
        return '读取归档内容' + (f"，偏移 {payload['offset']}" if type(payload.get('offset')) is int else '')
    if tool_name == 'workflows':
        return short_tool_invocation(redact_sensitive_value(str(payload.get('action') or '工作流')))
    for key in ("path", "url", "query", "artifact_ref", "root_id", "run_id", "status", "scope"):
        value = str(payload.get(key) or "").strip()
        if value:
            return short_tool_invocation(redact_sensitive_value(value))
    command = str(payload.get("command") or "").strip()
    if command:
        return short_tool_invocation(redact_sensitive_value(command))
    items = payload.get("items")
    if isinstance(items, list):
        return f"items={len(items)}"
    urls = payload.get("urls")
    if isinstance(urls, list):
        return f"urls={len(urls)}"
    return ""


def _text(value):
    return ' '.join(str(value or '').split())[:160]


def workflow_result_display(action, data, *, ok, error_code=''):
    """展示真实名称、范围和状态，不将整份 JSON 或内部调用引用当作预览。"""
    lines = ['工作流 · ' + _text(action) + ('：调用成功' if ok else '：失败')]
    if not ok:
        lines.append('原因：' + _text(data.get('error_code') or error_code or 'TOOL_EXECUTION_FAILED'))
    rows = data.get('items', data.get('workflows'))
    if isinstance(rows, list):
        lines.append(f'本页 {len(rows)} 个工作流' + ('；还有下一页' if data.get('next_cursor') else ''))
        for row in rows[:3]:
            if isinstance(row, dict):
                descriptor = row.get('descriptor') or row
                name = _text(descriptor.get('name')) if isinstance(descriptor, dict) else ''
                if name:
                    lines.append(name + (' · ' + _text(row['lifecycle']) if row.get('lifecycle') else ''))
    workflow = data.get('workflow', data.get('descriptor'))
    if isinstance(workflow, dict):
        descriptor = workflow.get('descriptor', workflow)
        if isinstance(descriptor, dict) and descriptor.get('name'):
            lines.append('名称：' + _text(descriptor['name']))
    scope = data.get('scope')
    if isinstance(scope, dict):
        mode = scope.get('scope_mode')
        title = {'owner_global': '本人全局', 'spaces': '指定业务空间', 'unassigned': '未分配'}.get(mode, mode)
        lines.append('范围：' + _text(title))
    state = data.get('state') or data.get('status')
    if state:
        lines.append('状态：' + _text(state))
    lines.append('按 Ctrl+E 查看本次完整结果')
    return {'kind': 'summary', 'summary': '\n'.join(lines[:6])}


def artifact_read_display(payload):
    """只展示归档读取窗口和完整性，call_id 等机器引用留在原结果中。"""
    if payload.get('ok') is not True:
        return {'kind': 'summary', 'summary': '归档读取失败：' + _text(payload.get('error_code'))}
    lines = [f"已读取归档：{payload.get('content_chars', 0)} 字符"]
    if payload.get('tool'):
        lines.append('来源工具：' + _text(payload['tool']))
    if type(payload.get('window_start')) is int and type(payload.get('window_end')) is int:
        lines.append(f"内容窗口：{payload['window_start']}–{payload['window_end']} / {payload.get('total_chars', '?')} 字符")
    if payload.get('has_more_after'):
        lines.append('后面还有内容，可继续分页读取')
    lines.append('按 Ctrl+E 查看本次原文')
    return {'kind': 'summary', 'summary': '\n'.join(lines)}
