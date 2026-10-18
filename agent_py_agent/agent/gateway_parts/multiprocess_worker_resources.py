# LLM: W复用scheduler索引业务实现，但绝不创建/迁移共享DB；正常带锁业务更新仍沿原类。
# 模块用途: 将共享scheduler数据库的existing-only初始化接到真实消费点。
import sqlite3

from ..scheduler.due_index import SchedulerDueIndex
from ..user_space.worker_home import existing_resource
from .multiprocess_bootstrap_resources import require_shared_resources


# LLM: 不复制schema或索引业务；构造只核已有文件，不连接SQLite，不触发WAL/schema初始化。
# 类用途: 保留原due-index接口，将W的共享库打开限定为已存在。
class WorkerSchedulerDueIndex(SchedulerDueIndex):
    # LLM: frozen保留原启动合同，数据库路径必须来自当前Agent已核的home_paths。
    # 函数用途: 只读验证共享DB后保存依赖，不调用普通父类的迁移构造。
    def __init__(self, frozen, paths):
        self._worker_bootstrap = frozen
        self.path = paths.global_index_dir / "scheduler_due.sqlite3"
        self._require_existing()

    # LLM: 每次消费再次核原根/分区及文件，缺失不能由sqlite隐式重建。
    # 函数用途: 用P4原no-follow资源门检查已有数据库。
    def _require_existing(self):
        frozen = self._worker_bootstrap
        resource = existing_resource("scheduler_due", self.path, "file")
        require_shared_resources(frozen, frozen.launch, (resource,))

    # LLM: 只允许打开已有库，业务BEGIN IMMEDIATE沿原实现；不调用ensure_schema或切换journal模式。
    # 函数用途: 为原scheduler索引操作提供mode=rw连接，不以缺库为新建许可。
    def _connect(self):
        self._require_existing()
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn
