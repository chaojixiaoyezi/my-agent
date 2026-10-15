# LLM: 部署级预算只记录UTC日期与已保留模型调用数，和owner日预算并守；没有逐员工线程或模型统计调用。
# 模块用途: 在共享原home根内限制所有owner自学总结总调用，损坏预算关闭新请求。
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from .skill_learning_store import SkillLearningStoreError


# LLM: 根目录由宿主注入，不能来自模型参数；缺文件是零，坏schema/计数不是无限额度。
# 函数用途: 在当前锁序下读取当天部署预算。
def _budget(root):
    path = Path(root)/'data'/'skill_learning_budget.json'
    report = read_json_object_report(path,context='skill_learning.global_budget')
    row = report.payload
    if report.load_error or (row and (row.get('schema_version')!='skill-learning-budget.v1' or isinstance(row.get('calls'),bool) or not isinstance(row.get('calls'),int) or row['calls']<0)):
        raise SkillLearningStoreError('SKILL_LEARNING_GLOBAL_BUDGET_UNAVAILABLE')
    today = datetime.now(timezone.utc).date().isoformat()
    calls = row.get('calls',0) if row.get('date')==today else 0
    return path,{'schema_version':'skill-learning-budget.v1','date':today,'calls':calls}


# LLM: 只读预检避免全局已满时反复消耗owner额度；真正保留仍需锁内重读。
# 函数用途: 判断是否值得进入owner计数事务，不调用模型。
def deployment_learning_available(root, limit):
    if root is None or limit<=0:
        return True
    _,row = _budget(root)
    return row['calls']<limit


# LLM: 所有owner共享同一文件锁，发模型请求前保留，失败也计数；0为明确不限。
# 函数用途: 原子占用一次部署级自学调用额度，防并发突破上限。
def reserve_deployment_learning(root, limit):
    if root is None or limit<=0:
        return True
    path = Path(root)/'data'/'skill_learning_budget.json'
    with locked_json_path(path):
        path,row = _budget(root)
        if row['calls']>=limit:
            return False
        row['calls']+=1
        write_json_file_atomic_unlocked(path,row)
    return True
