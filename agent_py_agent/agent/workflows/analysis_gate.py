"""按结构化查询健康事实阻止分析；不检查模型文本或另建状态。"""

import json


# LLM: tolerant reader只接受合同形状，缺键、非布尔和非法原因不触发阻断。
# 函数用途: 读取有效的query_health。
def query_health(value):
    if (isinstance(value, dict) and type(value.get('analysis_blocked')) is bool
            and 'block_reason' in value and (value['block_reason'] is None or isinstance(value['block_reason'], str))):
        return value
    return None


# LLM: 显式依赖优先，无映射时包含全部前步；成功恢复已过原合同门，不继承旧阻断或擦除诊断。
# 函数用途: 找到本次分析输入中仍未恢复的阻断来源，hybrid同时检查自身脚本结果。
def blocked_inputs(spec, records, own_record):
    dependencies = {ref['step_id'] for ref in spec.get('inputs_map', {}).values()
                    if isinstance(ref, dict) and isinstance(ref.get('step_id'), str) and ref['step_id'] != '$inputs'}
    path = spec.get('foreach', {}).get('from', '') if isinstance(spec.get('foreach'), dict) else ''
    if path and path.split('.')[0] != 'inputs':
        dependencies.add(path.split('.')[0])
    candidates = [row for row in records if row.step_id != own_record.step_id
                  and (not dependencies or row.step_id in dependencies)]
    if own_record.escalation_result.get('query_health') is not None:
        candidates.append(own_record)
    blocked = []
    for row in candidates:
        if row.status in ('succeeded', 'escalated_recovered') and row.escalation_result.get('recovered') is True:
            continue
        value = row.escalation_result.get('query_health')
        # 不同源的同号批没有关联保证；hybrid本批健康已保存在own_record。
        health = query_health(value)
        if health is not None and health['analysis_blocked'] is True:
            blocked.append((row.step_id, health))
    return blocked


# LLM: 只按健康结构裁决，不调用兜底模型；错误策略决定跳过、失败或暂停。
# 函数用途: 为被阻断分析记录准确错误、来源和取数失败输出。
def apply_analysis_gate(record, spec, records):
    blocked = blocked_inputs(spec, records, record)
    if not blocked:
        return False
    ids = []
    for step_id, health in blocked:
        for key in ('degraded_sources', 'auth_alerts'):
            entries = health.get(key, [])
            if isinstance(entries, list):
                ids.extend(item['source_id'] for item in entries if isinstance(item, dict) and isinstance(item.get('source_id'), str))
        sources = health.get('source_ids', [])
        if isinstance(sources, list):
            ids.extend(source for source in sources if isinstance(source, str))
    reason = blocked[0][1]['block_reason']
    record.error_code = 'ANALYSIS_BLOCKED_NO_DATA'
    record.error_message = '数据获取失败，未执行分析'
    record.output_snapshot = {'data_retrieval_failed': True, 'analysis_blocked': True,
                              'block_reason': reason, 'source_ids': sorted(set(ids)),
                              'input_step_ids': [step_id for step_id, _ in blocked]}
    record.escalation_result['query_health'] = {'analysis_blocked': True, 'block_reason': reason,
                                               'source_ids': sorted(set(ids))}
    policy = spec.get('on_error', 'escalate_to_agent')
    record.status = {'skip': 'skipped', 'fail': 'failed'}.get(policy, 'paused_for_resume')
    record.needs_repair = policy != 'skip'
    return True


# LLM: 隔离选择不授权工具或创建子代理，只构造交给原三参handler的请求字段。
# 函数用途: 生成C-ISOLATION参数并记录非法值替换。
def isolation_params(spec, record):
    configured = spec.get('config', {}).get('analysis_isolation', 'stateless_skill')
    if configured not in ('stateless_skill', 'lightweight_subagent'):
        record.escalation_result['analysis_isolation_replaced'] = True
        configured = 'stateless_skill'
    return json.loads(json.dumps({'analysis_isolation': configured}))
