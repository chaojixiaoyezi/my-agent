"""Owner 权威 runtime.db（3.txt A 节）。

每个 owner 只有一个权威 runtime.db（A.1），位于 owner home 根；
local_storage 的 records/agent_runs/agent_events/task_rollups 都是投影
（A.2），不得反向决定权威状态。旧 local_storage.agent_runs 已改名
legacy_agent_runs（R1：新旧不得同名双权威）。
"""
