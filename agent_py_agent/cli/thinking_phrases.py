from __future__ import annotations

import random as _random

PHRASES = {
    "context": ["分析中", "整理上下文"],
    "tool": ["读取结果", "处理工具结果"],
    "state": ["检查状态", "准备下一步"],
    "model": ["等待模型返回", "汇总进度"],
}


def random_phrase() -> str:
    phrases = [phrase for group in PHRASES.values() for phrase in group]
    return _random.choice(phrases)
