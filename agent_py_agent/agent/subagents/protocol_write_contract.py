# LLM: Protocol write-contract helpers keep task handoff boundaries separate from envelope assembly.
# 模块用途: 从 SubAgentTask 的结构化字段生成写入合同，避免协议主文件承载路径过滤和授权合并细节。

from __future__ import annotations

from .models import SubAgentTask

_FILESYSTEM_WRITE_GRANT_TOOLS = {"write_file", "apply_patch"}


# LLM: build_write_contract keeps path boundaries in one protocol field.
# 函数用途: 暴露允许写入、禁止写入和锁定文件，供 preflight/recovery/QA 共用。
def build_write_contract(task: SubAgentTask) -> dict[str, object]:
    allowed_roots = _effective_allowed_write_roots(task)
    return {
        "internal_task_root": _task_text(task, "task_dir"),
        "product_write_roots": _product_write_roots(task, allowed_roots),
        "allowed_write_roots": allowed_roots,
        "forbidden_write_roots": _task_list(task, "forbidden_write_roots"),
        "locked_files": _task_list(task, "locked_files"),
    }


# LLM: _product_write_roots separates user deliverable roots from the run's private scratch space.
# 函数用途: 过滤掉子代理自己的内部任务目录，避免把能写日志误判成能写用户产物。
def _product_write_roots(task: SubAgentTask, allowed_roots: list[str]) -> list[str]:
    internal_roots = _internal_roots(task)
    task_root = _normalize_path(_task_text(task, "task_dir"))
    result: list[str] = []
    for root in allowed_roots:
        normalized = _normalize_path(root)
        if not _is_product_root(normalized, task_root, internal_roots):
            continue
        result.append(root)
    return result


# LLM: _internal_roots collects task-private roots that should not count as product outputs.
# 函数用途: 列出 data/output/tests/reports/logs/scratch 等内部目录，用于产品写入根过滤。
def _internal_roots(task: SubAgentTask) -> set[str]:
    names = ("task_dir", "data_dir", "output_dir", "tests_dir", "reports_dir", "logs_dir", "scratch_dir")
    return {_normalize_path(_task_text(task, name)) for name in names}


# LLM: _is_product_root applies product-root filtering without touching the filesystem.
# 函数用途: 判断一个授权目录是否是真正给用户产物用，而不是子代理内部运行目录。
def _is_product_root(normalized: str, task_root: str, internal_roots: set[str]) -> bool:
    if not normalized or normalized in internal_roots:
        return False
    return not (task_root and normalized.startswith(f"{task_root}/"))


# LLM: _normalize_path gives protocol checks stable string comparisons without touching the filesystem.
# 函数用途: 规范化路径字符串用于内部目录过滤；不存在的目录也不会报错。
def _normalize_path(value: str) -> str:
    return str(value or "").rstrip("/")


# LLM: _effective_allowed_write_roots keeps TaskEnvelope aligned with the actual filesystem gateway.
# 函数用途: 把已批准的文件写入 grant.path_scope 同步进协议写边界，避免 runner 合同和真实工具边界不一致。
def _effective_allowed_write_roots(task: SubAgentTask) -> list[str]:
    roots = _task_list(task, "allowed_write_roots")
    for grant in _task_list(task, "capability_grants"):
        if _grant_allows_filesystem_write(grant):
            roots = _merge_unique([*roots, *_grant_path_scope(grant)])
    return roots


# LLM: _grant_allows_filesystem_write reads only structured grant tool names.
# 函数用途: 只有通用文件写入授权才会扩展普通文件写入边界；shell grant 仍走 controlled_exec。
def _grant_allows_filesystem_write(grant: object) -> bool:
    tools = {str(item or "").strip() for item in getattr(grant, "tools", []) or []}
    return bool(tools & _FILESYSTEM_WRITE_GRANT_TOOLS)


# LLM: _grant_path_scope normalizes grant.path_scope without interpreting prose.
# 函数用途: 从结构化授权里取路径列表；坏对象或空字符串直接忽略。
def _grant_path_scope(grant: object) -> list[str]:
    value = getattr(grant, "path_scope", [])
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, (list, tuple, set)) else []


# LLM: _merge_unique preserves write-root order while de-duping repeated grants.
# 函数用途: 稳定协议输出，避免同一个授权目录重复进入 prompt 和测试快照。
def _merge_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# LLM: _task_text reads task-like objects defensively for legacy mocks and persisted tasks.
# 函数用途: 兼容真实 SubAgentTask 和旧测试替身；缺字段时返回空字符串而不是抛异常。
def _task_text(task: object, name: str) -> str:
    return str(getattr(task, name, "") or "")


# LLM: _task_list normalizes list-like task fields without mutating the task object.
# 函数用途: 读取授权、路径和锁定文件列表；缺字段或非列表时返回空列表。
def _task_list(task: object, name: str) -> list:
    value = getattr(task, name, [])
    return list(value) if isinstance(value, (list, tuple, set)) else []
