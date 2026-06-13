# LLM: 来源比例观测(REFACTORING_BACKLOG"交付数据量 vs 来源调用量",实锤 R8b
#   接力轮隐蔽编造:24/24 xlsx + closeout ok=true,但生成脚本把同一份 19 项静态
#   列表复用 24 周、周增长=公式编造——模型汇报说诚实、代码里藏编造)。设计裁决:
#   ①机器判不了单个数字真假(既有裁决),也判不了"数据类产物 vs 分析类产物"
#   (内容语义)——任何可疑阈值都必然误伤纯本地任务(零网络+15 个分析 md 是
#   正常形态),因此**只做观测投影、零 finding、零拦截**:把"网络检索调用量"与
#   "交付规模"两组客观计数并排放进 closeout 报告,比例是否可疑由把关者(人/上级
#   代理)结合任务性质判断;②检索侧零工具白名单——按 ToolSpec.category=="web"
#   结构化判定(开放世界:新检索工具标对 category 自动纳入);shell 内的网络访问
#   无法结构化判定,口径字段如实声明 network_calls_scope="web_category_tools"。
#   改动时同步检查 uncontracted.py / closeout.py 两个投影点与
#   tests/test_source_volume_observation.py。
# 模块用途: 给验收报告加一组"交付了多少 vs 真实检索了多少"的并排数字,把关者
#   一眼能看出"交付 480 个数据点但只查了 19 次"这类比例异常,机器自己不下结论。
from __future__ import annotations

from pathlib import Path
from typing import Any

# 单文件字节统计的安全上限:超大文件按上限计,防止 stat 异常值撑爆报告语义。
_MAX_FILE_BYTES_COUNTED = 100 * 1024 * 1024


# LLM: 观测构造唯一入口。纯函数(只读 archive 与文件系统 stat),永不抛异常,
#   永远返回完整字段(取不到的计 0/空),不做任何"可疑"判定。
# 函数用途: 数三组客观数字:网络检索成功了几次、交付了几个文件多少字节、
#   声明了至少要交几个。
def source_volume_observation(
    archive_tool_calls: list | None,
    artifact_paths: list[str],
    *,
    web_tool_names: frozenset[str],
    declared_min_count_total: int = 0,
) -> dict[str, Any]:
    network_calls = _network_success_calls(archive_tool_calls, web_tool_names)
    delivered_files, delivered_bytes = _delivered_volume(artifact_paths)
    return {
        "schema_version": "source_volume_observation.v1",
        "network_success_calls": network_calls,
        "network_calls_scope": "web_category_tools",
        "delivered_files": delivered_files,
        "delivered_bytes": delivered_bytes,
        "declared_min_count_total": int(declared_min_count_total or 0),
        "note_zh": (
            "纯观测并排数字，不构成判定：交付规模与检索量的比例是否合理，"
            "取决于任务性质（本地分析类任务零网络调用是正常形态），由把关者判断。"
        ),
    }


# LLM: 检索侧计数:archive ok=true 且 tool 名属于 web 类 spec 集合(调用方从
#   registry specs 按 category=="web" 收集,本函数不碰 registry)。
# 函数用途: 数本轮真实成功的网络检索调用次数。
def _network_success_calls(archive_tool_calls: list | None, web_tool_names: frozenset[str]) -> int:
    if not web_tool_names:
        return 0
    count = 0
    for record in archive_tool_calls or []:
        if not isinstance(record, dict) or record.get("ok") is not True:
            continue
        if str(record.get("tool") or "").strip() in web_tool_names:
            count += 1
    return count


# 函数用途: 数交付文件个数与总字节(文件不存在/不可 stat 的跳过,不猜)。
def _delivered_volume(artifact_paths: list[str]) -> tuple[int, int]:
    files = 0
    total_bytes = 0
    for raw in artifact_paths or []:
        size = _file_size_or_none(Path(str(raw or "")).expanduser())
        if size is None:
            continue
        files += 1
        total_bytes += min(size, _MAX_FILE_BYTES_COUNTED)
    return files, total_bytes


# 函数用途: 安全取单文件字节数;不是文件或 stat 失败返回 None。
def _file_size_or_none(path: Path) -> int | None:
    try:
        if not path.is_file():
            return None
        return int(path.stat().st_size)
    except OSError:
        return None


# LLM: web 类工具名集合的唯一解析口(开放世界:按 spec.category 判,零名单)。
#   registry 不可用/异常时返回空集(观测计 0,不崩验收)。
# 函数用途: 从工具注册表里找出"是网络检索类"的工具名集合。
def web_category_tool_names(agent: Any) -> frozenset[str]:
    try:
        specs = list(agent.tools.specs() or [])
    except Exception:
        return frozenset()
    return frozenset(name for spec in specs if (name := _web_spec_name(spec)))


# 函数用途: spec 是 web 类时返回工具名,否则空串。
def _web_spec_name(spec: Any) -> str:
    if str(getattr(spec, "category", "") or "").strip().lower() != "web":
        return ""
    return str(getattr(spec, "name", "") or "").strip()


# LLM: closeout 报告投影的组装入口(uncontracted 与 contract 双路径共用)。
#   交付侧取 report["artifacts"] 的 ok 文件(产物候选权威,compact 失明已由磁盘
#   扫描兜底修复);声明侧读 task_progress 的 expected_outputs(与对账门同源)。
#   任何一步异常都不打断验收(观测缺席好过验收崩溃),字段计 0。副作用:只写
#   report["source_volume_observation"] 一个键。
# 函数用途: 验收收尾时把"检索量 vs 交付量"的并排数字挂进报告。
def attach_source_volume_observation(closeout: Any, report: dict[str, Any]) -> None:
    try:
        artifact_paths = [
            str(item.get("path") or "")
            for item in (report.get("artifacts") or [])
            if isinstance(item, dict) and item.get("ok") is True
        ]
        report["source_volume_observation"] = source_volume_observation(
            list(getattr(getattr(closeout, "params", None), "archive_tool_calls", []) or []),
            artifact_paths,
            web_tool_names=web_category_tool_names(getattr(closeout, "agent", None)),
            declared_min_count_total=_declared_min_count_total(closeout),
        )
    except Exception:
        report["source_volume_observation"] = source_volume_observation(
            [], [], web_tool_names=frozenset(), declared_min_count_total=0
        )


# 函数用途: 从 progress 账本汇总 expected_outputs 的 min_count 总数(取不到计 0)。
def _declared_min_count_total(closeout: Any) -> int:
    from ...task_progress import read_task_progress
    from .task_progress_gate import _progress_root, _run_id

    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return 0
    total = 0
    for entry in read_task_progress(root, run_id).get("expected_outputs") or []:
        if isinstance(entry, dict):
            total += max(1, int(entry.get("min_count") or 1))
    return total


__all__ = [
    "attach_source_volume_observation",
    "source_volume_observation",
    "web_category_tool_names",
]
