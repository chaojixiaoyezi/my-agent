# LLM: 这里只读原 RuntimeDB/canonical 控制事实，不新建取消状态；异进程心跳和注册后准入复用同一判据。
# 模块用途: 让 runner 在自己的进程里识别原执行轮已被停止，避免按共享 PID 杀掉其它任务。
from __future__ import annotations


# LLM: 只把明确取消、UNKNOWN、废弃或代次替换当原轮失效；自然完成/失败不在正常结果提交途中制造中断。
# 查询错误由调用方保留，不能把暂时读失败当取消；空身份的 dry run 不受此入口控制。
# 函数用途: 从唯一状态源判断原 runner 是否应停止，无写入、信号或新执行副作用。
def runner_attempt_cancelled(manager: object, run_id: str, attempt_id: str) -> bool:
    if not attempt_id:
        return False
    repo = manager.runtime_db
    if repo is not None:
        row = repo.agent_run_for_run_id(run_id)
        if row is None:
            raise RuntimeError("runner 原执行权记录不可读")
        if str(row["current_attempt_id"] or "") != attempt_id:
            return True
        attempt = repo.get_attempt(attempt_id)
        if attempt is None:
            raise RuntimeError("runner 原执行轮不可读")
        if attempt["status"] in {"cancelled", "unknown", "abandoned"}:
            return True
    task = manager.load(run_id)
    return (
        task.status in {"CANCELLED", "ABANDONED", "TAKEN_OVER"}
        or attempt_id in task.runner_abandoned_attempt_ids
        or bool(task.runner_active_attempt_id and task.runner_active_attempt_id != attempt_id)
    )
