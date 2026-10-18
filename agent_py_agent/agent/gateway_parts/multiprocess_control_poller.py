# LLM: 控制轮询独立于前台/后台队列和心跳；组合根须先保存lane再start，ACK异常仍持原Thread收尾，不冒充控制业务完成。
# 模块用途: 为worker提供半秒控制轮询通道，启动前保留句柄，停止时真实join同一线程。
import threading


# LLM: 只轮询已经冻结的控制泵，不改路由/容量/owner；泵内部短锁防重复认领。
# 类用途: 即使前台队列慢也及时读取同分区控制。
class ControlPollingLane:
    # LLM: 构造无线程启动副作用；调用方先持有本对象再start，并在所有退出路径close，不能因ACK异常丢原Thread。
    # 函数用途: 准备独立控制轮询的唯一原线程，尚不启动。
    def __init__(self, controls, worker_stop):
        self.controls, self.worker_stop = controls, worker_stop
        self._stop = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, name='gateway-worker-control-poll')
        self._start_attempted = False

    # LLM: 只尝试启动一次原Thread；异常不能证明未出生，先唤醒worker停止并保留原引用给close，不自动重试或创建替代线程。
    # 函数用途: 在组合根已经保存lane之后启动轮询，启动确认异常触发同一worker收尾。
    def start(self):
        if self._start_attempted:
            raise RuntimeError('control_poll_start_already_attempted')
        self._start_attempted = True
        try:
            self._thread.start()
        except BaseException:
            self.worker_stop.set()
            self._stop.set()
            raise

    # LLM: 原异常保留给组合根；关闭worker认领不代表原业务或六类资源已排空。
    # 函数用途: 半秒一次轮询，失败通知worker退出。
    def _run(self):
        try:
            while not self._stop.is_set() and not self.worker_stop.is_set():
                self.controls.poll()
                self._stop.wait(0.5)
        except Exception as error:  # noqa: BLE001 - 不让线程异常静默消失。
            self._error = error
            self.worker_stop.set()

    # LLM: 从未尝试可直接关闭；已尝试须join原Thread，ACK未知的join异常仍传播，不能按Event猜dead；调用方另行关闭控制槽。
    # 函数用途: 等同一轮询线程退出并传播实际失败，不把轮询退出当控制业务结清。
    def close(self):
        self._stop.set()
        if not self._start_attempted:
            return
        self._thread.join()
        if self._error is not None:
            raise self._error
