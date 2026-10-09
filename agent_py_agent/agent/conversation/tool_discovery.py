# LLM: 加载名是线程展示事实，不是权限；只读正式搜索信封并原子并入线程，不从摘要正文反推或写Agent全局状态。
# 模块用途: 在压缩移走搜索记录后仍让同一线程的下一回合恢复工具表。
from dataclasses import replace

from ..settings.reasoning_effort import run_thread_id
from ..tooling.tool_search_state import pending_carried_loaded_tool_names


# LLM: 只接受持久化名称字符串，损坏字段不转换成新工具；实际授权仍由当前注册表快照过滤。
# 函数用途: 归一线程的工具名集合，不执行工具。
def normalized_loaded_tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(sorted({item.strip() for item in value if isinstance(item, str) and item.strip()}))


# LLM: 与路由身份共用子线程优先级；无线程或尚未创建时不借用其它线程、归档或Agent集合。
# 函数用途: 为新回合读取本线程已加载工具名。
def thread_loaded_tools(agent: object, params: object) -> set[str]:
    thread_id = run_thread_id(params)
    store = getattr(agent, "conversation_store", None)
    if not thread_id or store is None:
        return set()
    thread = store.threads.load(thread_id)
    return set(normalized_loaded_tools(getattr(thread, "loaded_tool_names", ())))


# LLM: 只从同一次真实搜索的typed结果取名，update_atomic保留并发模型设置和Compact代次；缺线程不新建。
# 函数用途: 把搜索发现原子记入所属线程，压缩或新回合不会丢失。
def persist_discovered_tools(agent: object, params: object, record: dict[str, object]) -> None:
    names = pending_carried_loaded_tool_names([record])
    thread_id = run_thread_id(params)
    store = getattr(agent, "conversation_store", None)
    if not names or not thread_id or store is None:
        return

    def merge(thread):
        loaded = normalized_loaded_tools((*thread.loaded_tool_names, *names))
        return replace(thread, loaded_tool_names=loaded) if loaded != thread.loaded_tool_names else thread

    try:
        store.threads.update_atomic(thread_id, merge)
    except FileNotFoundError:
        # 尚未创建或已经移除的线程不补造；其它存储失败仍显式传播。
        return
