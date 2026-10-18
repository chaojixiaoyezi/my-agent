# LLM: Linux组长Z不代表线程组退出；只读status的严格Threads/Pid/Tgid，外层stat复核身份后才接受。
# 模块用途: 保留组长原始Z状态，单独记录线程组是否仍可能执行，读取失败不冒充停止。


# LLM: pthread_exit后task可能不可遍历，使用任务允许的Threads替代；大于1保守live，不虚构线程stat。
# 函数用途: 读取Z组长的线程数，任何坏字段/权限失败都沿外层unknown路径。
def thread_group_live(entry, pid: int) -> bool:
    lines = (entry / "status").read_text(encoding="utf-8").splitlines()
    fields = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in {"Pid", "Tgid", "Threads"}:
            _status_field(fields, key, value)
    if set(fields) != {"Pid", "Tgid", "Threads"} or fields["Pid"] != pid or fields["Tgid"] != pid:
        raise ValueError("thread_group_identity_unconfirmed")
    return fields["Threads"] > 1


# LLM: 不能把缺/重复/零/负数/非ASCII值默认成单线程；纯解析，不写文件。
# 函数用途: 校验一个明确的线程组身份字段。
def _status_field(fields: dict, key: str, value: str) -> None:
    text = value.strip()
    if key in fields or not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError("thread_group_status_invalid")
    fields[key] = int(text)
