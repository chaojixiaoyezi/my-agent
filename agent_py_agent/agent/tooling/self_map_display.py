# LLM: 只压缩已经准入的公共元信息；状态、权限和计数判定来自原结构化读侧，不解析用途文字作裁决。
# 模块用途: 给自我目录提供短行、共享精确详情模板及数量/完整性说明，不读盘或保存状态。
from .self_map_registry import next_read


# LLM: 仅共享相同显示值；领域已准入的完整路径、目录标签和模型类型不能被短行丢掉，不从ID猜实际目录或档案身份。
# 函数用途: 声明短行列顺序，保留精确详情入口、路径和类别；重复显示字段只放一份。
def compact_columns(items):
    optional = ("directory_label", "path", "entry_type", "read_access", "write_access", "unknown_reason")
    names = [] if items and all(row.get("name") == row["id"] for row in items) else ["name"]
    purposes = [] if items and len({_brief_purpose(row) for row in items}) == 1 else ["summary"]
    state = ["state"] if any(row.get("state", "known") != "known" for row in items) else []
    return ["next_read", *names, *purposes, *state, *(key for key in optional if any(key in row for row in items))]


# LLM: ID和名称原样保留；用途只作显示截短且空摘要沿原用途回退，不用于授权/分类。详情沿共享模板保留原字段。
# 函数用途: 生成可读短行，保留每条的精确详情参数和状态。
def compact_item(item, columns):
    values = item | {"next_read": {"item": item["id"]}, "summary": _brief_purpose(item), "state": item.get("state", "known")}
    return [values.get(key) for key in columns]


# LLM: 只规范显示首句；空白/纯标点候选退回原purpose或静态说明，不改详情、来源准入或机器权限判定。
# 函数用途: 防止换行、空白或省略号占位摘要吞掉一条本来有效的用途。
def _brief_purpose(item):
    for value in (item.get("summary"), item.get("purpose"), "公开元信息，详情见入口。"):
        text = str(value or "").strip()
        brief = " ".join(text.split("。", 1)[0].split("\n", 1)[0].split())[:24]
        if any(char.isalnum() for char in brief):
            return brief


# LLM: next_read仍是完整item参数；只共享经逐项等价确认的名称/短用途，不截身份或原详情、不猜业务类别。
# 函数用途: 给出无损显示默认值和精确详情模板，让长同名条目不重复占满原输出预算。
def compact_metadata(section, columns, items):
    defaults = {"state": "known"}
    if items and "summary" not in columns:
        defaults["summary"] = _brief_purpose(items[0])
    name = {"item_name_from": "next_read.item"} if "name" not in columns else {}
    return {"item_columns": columns, "item_defaults": defaults, **name, "row_id": "next_read.item", "detail_complete": False,
            "detail_next_read": next_read("self_map", {"section": section})}


# LLM: 来源不完整时只能说当前已观测条目列完；剩余数是本次公共快照项数，不当作完整业务库存数。
# 函数用途: 让调用方知道本页已列全还是还剩几条，避免盲目翻页。
def coverage_summary(projection, tail):
    remaining = len(projection.items) - tail
    suffix = "（仅已观测条目，来源仍不完整）" if projection.completeness != "complete" else ""
    return (f"还有{remaining}条" if remaining else "本页已列全") + suffix + "。"


# LLM: 计数沿结构化counts/count_known/completeness；只有known空集显示当前为空，不能抹掉unavailable或not_observed。
# 函数用途: 每域一行数量和原状态，让空总览答数量但不把失败或未知说成已知空。
def status_summary(projection):
    known = projection.count_known and projection.completeness == "complete"
    count = str(len(projection.items)) if known else "未统计"
    parts = ["条目" + count]
    parts.extend(projection.count_labels.get(key, key) + (str(value) if value is not None else "未统计")
                 for key, value in projection.counts.items())
    state = "当前为空" if known and not projection.items and projection.state == "known" else projection.state
    return projection.title + "：" + "、".join(parts) + "；" + state + "。"
