# LLM: 仅同进程域及精确出生核对；kill(0)只查询存在，不发送信号，不可见/拒绝保持unknown。
# 模块用途: 对原控制认领的执行者做三态存活查询，不把心跳超时当死亡。
import os

from .daemon_metadata import process_host_id
from .multiprocess_executor_identity import process_birth
from .multiprocess_records import MultiprocessRecordError, validate_record


# LLM: 单进程查询不是执行树排空证据，不授予重放；同域PID不存在或已复用才确认旧执行者死亡。
# 函数用途: 返回控制执行者live/dead/unknown三态。
def control_executor_live(identity):
    try:
        identity = validate_record(identity)
    except MultiprocessRecordError:
        return None
    if identity['schema_version'] != 'multiprocess_process_identity.v1' or identity['host_id'] != process_host_id():
        return None
    try:
        os.kill(identity['pid'], 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return None
    birth = process_birth(identity['pid'])
    return None if birth is None else birth == identity['start_time']
