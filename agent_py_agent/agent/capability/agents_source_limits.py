# LLM: AGENTS 所有最终仓库提交共用硬帽；调用方先持来源/人格锁，不能把提示截断当成写入限制。
# 模块用途: 拦住超过注入预算的新增，同时让历史超限正文仍能受控缩小。

AGENTS_WRITE_MAX_CHARS = 20_000


# LLM: 在任何正文/备份/账本落盘前核对写后原文字符数；历史超限只允许严格净缩减，无I/O。
# 函数用途: 给本地来源和原人格事务使用相同的写入硬上限，失败不改变文件。
def enforce_agents_write_limit(previous: str, next_text: str, max_chars: int = AGENTS_WRITE_MAX_CHARS) -> None:
    limit = min(AGENTS_WRITE_MAX_CHARS, max_chars)
    if len(next_text) > limit and len(next_text) >= len(previous):
        raise ValueError("AGENTS_WRITE_LIMIT")
