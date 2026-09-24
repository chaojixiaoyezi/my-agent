# LLM: 此集合只保存近期显示去重身份，不是任务账或持久游标；业务执行不得依赖它是否仍记得旧身份。
# 模块用途: 让长期开着的 TUI 不因每条消息的去重编号持续增长内存。
from __future__ import annotations

from collections import OrderedDict
from collections.abc import MutableSet


# LLM: 保持集合接口，按最近登记顺序淘汰；历史真实性仍由 canonical 消息及显式分页游标提供。
# 类用途: 给显示层提供固定容量的近期编号集合。
class TuiIdentityWindow(MutableSet):
    # LLM: 容量仅影响短期显示去重，不截断正文或更改存储；无 I/O。
    # 函数用途: 创建空身份窗口。
    def __init__(self, values=(), *, maximum: int = 40_000):
        self.maximum = max(1, int(maximum))
        self._items = OrderedDict()
        for value in values:
            self.add(value)

    # LLM: 只按结构化 ID 相等查询，不解析文字。
    # 函数用途: 判断近期是否登记过这个编号。
    def __contains__(self, value):
        return value in self._items

    # LLM: 迭代次序仅为显示缓存登记顺序，不是业务执行顺序。
    # 函数用途: 遍历当前有界身份。
    def __iter__(self):
        return iter(self._items)

    # LLM: 只返回缓存条数，不代表历史消息总数。
    # 函数用途: 读取当前占用。
    def __len__(self):
        return len(self._items)

    # LLM: 同身份登记刷新近期位置，超额只淘汰编号；不能落盘或驱动任务动作。
    # 函数用途: 记住最近展示过的身份并释放过旧编号。
    def add(self, value):
        self._items[value] = None
        self._items.move_to_end(value)
        while len(self._items) > self.maximum:
            self._items.popitem(last=False)

    # LLM: 丢弃是显示缓存操作，不是撤回消息或取消任务。
    # 函数用途: 移除一个不再需要的近期编号。
    def discard(self, value):
        self._items.pop(value, None)
