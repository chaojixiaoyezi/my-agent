# LLM: 只调整宿主归档记录的模型视图，不改原工具JSON/信封、完整归档或授权/执行事实。
# 模块用途: 让已经分页和裁剪过的包页面保留完整导航与复制引用，不再被通用日志预览截断。
from __future__ import annotations

import json


# LLM: 仅注册的skill_search成功包页面普通外置适用；宿主显式外置原因优先，硬余量不得重新内联。
# 函数用途: 在真实归档生成后保留读取器声明的有界页面和原逻辑恢复锚点。
def preserve_package_page_view(outcome, record: dict[str, object]) -> None:
    if outcome.tool != 'skill_search' or not outcome.ok or record.get('output_externalized') is not True:
        return
    # 显式宿主外置约束（含硬余量）保持原投影；未知的未来原因也不能被页面配置覆盖。
    if record.get('output_externalized_reason'):
        return
    try:
        payload = json.loads(outcome.output)
    except (TypeError, ValueError):
        return
    namespace = payload.get('resource_namespace') if isinstance(payload, dict) else None
    if (not isinstance(namespace, dict) or namespace.get('kind') != 'capability_package'
            or namespace.get('declared_members_only') is not True or namespace.get('filesystem_path') is not False):
        return
    # 保留producer已经按原读取/预览预算生成的完整JSON，而非1200字符的日志摘录。
    # 原归档仍保存完整原文；恢复字段由原reducer按真实scoped_call_id生成，不在这里猜引用。
    record['live_prompt_output'] = outcome.model_visible_output()
    record['preserve_prompt_output'] = True
