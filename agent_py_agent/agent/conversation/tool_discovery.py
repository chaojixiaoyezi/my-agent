# LLM: 加载名是线程展示事实，不是权限；只读正式搜索信封并原子并入线程，不从摘要正文反推或写Agent全局状态。
# 模块用途: 在压缩移走搜索记录后仍让同一线程的下一回合恢复工具表。
from dataclasses import replace

from ..settings.reasoning_effort import run_thread_id
from ..tooling.tool_search_state import ordered_carried_loaded_tool_names, ordered_tool_names


# LLM: 新序列保留首次发现顺序，旧集合使用统一稳定兜底；损坏字段不变成新工具，授权仍由当前快照过滤。
# 函数用途: 归一线程的有序工具名，不执行工具。
def normalized_loaded_tools(value: object) -> tuple[str, ...]:
    return ordered_tool_names(value)


# LLM: 与路由身份共用子线程优先级；无线程或尚未创建时不借用其它线程、归档或Agent集合。
# 函数用途: 为新回合读取本线程已加载工具名。
def thread_loaded_tools(agent: object, params: object) -> set[str]:
    thread_id = run_thread_id(params)
    store = getattr(agent, "conversation_store", None)
    if not thread_id or store is None:
        return set()
    thread = store.threads.load(thread_id)
    return set(normalized_loaded_tools(getattr(thread, "loaded_tool_names", ())))


# LLM: typed 发现按顺序原子写所属线程；独立 metadata 历史只增不减，活跃字段可由显式边界另选，缺线程不新建。
# 函数用途: 保留完整发现历史和当前加载顺序，不覆盖并发模型设置、Compact 代次或其它 metadata。
def persist_discovered_tools(agent: object, params: object, record: dict[str, object]) -> None:
    names = ordered_carried_loaded_tool_names([record])
    thread_id = run_thread_id(params)
    store = getattr(agent, "conversation_store", None)
    if not names or not thread_id or store is None:
        return

    # LLM: 回调读 update_atomic 提供的最新线程，拷贝 metadata 后合并，不热改传入对象。
    # 函数用途: 在原线程上一起保存有序加载名与只增不减的发现历史。
    def merge(thread):
        loaded = normalized_loaded_tools((*thread.loaded_tool_names, *names))
        metadata = dict(thread.metadata)
        history = normalized_loaded_tools(metadata.get("discovered_tool_history", ()))
        metadata["discovered_tool_history"] = list(normalized_loaded_tools((*history, *thread.loaded_tool_names, *names)))
        return replace(thread, loaded_tool_names=loaded, metadata=metadata) if (
            loaded != thread.loaded_tool_names or metadata != thread.metadata) else thread

    try:
        store.threads.update_atomic(thread_id, merge)
    except FileNotFoundError:
        # 尚未创建或已经移除的线程不补造；其它存储失败仍显式传播。
        return
