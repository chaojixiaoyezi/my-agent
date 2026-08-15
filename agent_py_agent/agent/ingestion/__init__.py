"""高吞吐摄取层:在「原始数据流」和「LLM 研判」之间做代码层预聚合/初筛/降噪/背压。

把每秒上百条的结构化事件压成「每批几条」的候选批,LLM 只按批研判。

【铁律】本层只用结构化信号做降维:字段路径/类型、值基数(去重键)、滑动窗口计数、
频次阈值、单调性、数量级分桶、采样与预算上限。**严禁任何自然语言/关键词判断来定性
"是不是目标事件"**——那个判断永远留给模型;本层只负责"把重复/高频的压成组+示例、
把结构化稀有的抬上来",做的是降维不是定性。所有被压掉的都有如实账目(覆盖游标区间、
每组计数、溢出清单),不假装全看了。
"""

from .config import IngestTuning, tuning_from_params
from .engine import CallDigest, StreamDigestEngine

__all__ = [
    "CallDigest",
    "IngestTuning",
    "StreamDigestEngine",
    "tuning_from_params",
]
