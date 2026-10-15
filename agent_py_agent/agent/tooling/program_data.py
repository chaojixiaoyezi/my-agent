# LLM: 只解释有界JSON数据与JSON Pointer，不执行表达式/代码/属性访问；转换不授权调用，失败与覆盖始终由外层保留。
# 模块用途: 给受限工具程序提供引用、筛选、去重、分组统计和分页。
from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from threading import Lock


# LLM: 同一展开/并发捕获共用字节账，先流式计量再复制；锁只保护预算，不持锁执行工具或网络。
# 类用途: 限制引用展开与完整JSON捕获的累计内存规模。
class DataBudget:
    # LLM: 限制来自宿主常量，不接受模型增加额度。
    # 函数用途: 初始化一个阶段的累计字节预算。
    def __init__(self, limit=2_000_000):
        self.remaining = limit
        self.lock = Lock()

    # LLM: iterencode避免先构造完整序列化字符串；超过预算在深拷贝/解析前失败。
    # 函数用途: 原子预留JSON值或已脱敏正文占用的字节数。
    def reserve(self, value, *, text=False):
        chunks = (value,) if text else json.JSONEncoder(ensure_ascii=False).iterencode(value)
        with self.lock:
            for chunk in chunks:
                if len(chunk) > self.remaining:
                    self.remaining = -1
                    raise ValueError('中间数据超过预算')
                self.remaining -= len(chunk.encode())
                if self.remaining < 0:
                    raise ValueError('中间数据超过预算')


# LLM: 路径只在已持有的JSON对象内取值，禁止文件/URL解析、负索引与Python属性；缺失必须显式失败。
# 函数用途: 按标准转义JSON Pointer读取一个确定值。
def pointer(value, path):
    if path == '':
        return value
    if not isinstance(path, str) or not path.startswith('/') or len(path) > 1024:
        raise ValueError('JSON Pointer无效')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            value = value[part]
        elif isinstance(value, list) and part.isascii() and part.isdecimal():
            value = value[int(part)]
        else:
            raise ValueError('JSON路径不存在')
    return value


# LLM: 引用只可读此前完成step或当前item，不允许未来依赖、外部路径和身份插值；共用预算先计量再深拷贝避免放大或改原证据。
# 函数用途: 将参数中的显式引用替换为确定数据，普通字符串不作模板解释。
def resolve(value, values, item=None, depth=0, budget=None):
    budget = budget or DataBudget()
    if depth > 16:
        raise ValueError('程序参数嵌套过深')
    if isinstance(value, dict):
        if set(value) == {'$ref'}:
            name, path = value['$ref'].split('#', 1)
            selected = pointer(item if name == 'item' else values[name], path)
            budget.reserve(selected)
            return deepcopy(selected)
        return {key: resolve(val, values, item, depth+1, budget) for key, val in value.items()}
    if isinstance(value, list):
        return [resolve(val, values, item, depth+1, budget) for val in value]
    budget.reserve(value)
    return value


# LLM: 转换规则是有限枚举，原数量、过滤数量、去重与页范围分开报告；不从自然语言推断零结果或完整性。
# 函数用途: 筛选/去重/投影一页或统计分组，并返回可核对覆盖信息。
def project(rows, options):
    allowed = {'flatten', 'where', 'unique_by', 'fields', 'offset', 'limit', 'count_by'}
    if not isinstance(rows, list) or set(options) - allowed:
        raise ValueError('转换需要数组和准确选项')
    selected, total = rows, len(rows)
    if 'flatten' in options:
        expanded = [pointer(row, options['flatten']) for row in rows]
        if any(not isinstance(value, list) for value in expanded):
            raise ValueError('展开目标必须全部是数组')
        selected = [item for value in expanded for item in value]
    expanded_count = len(selected)
    if 'where' in options:
        where = options['where']
        if not isinstance(where, dict) or set(where) != {'path', 'equals'}:
            raise ValueError('筛选条件无效')
        selected = [row for row in selected if pointer(row, where['path']) == where['equals']]
    matched = len(selected)
    if 'unique_by' in options:
        unique, seen = [], set()
        for row in selected:
            key = json.dumps(pointer(row, options['unique_by']), sort_keys=True, ensure_ascii=False)
            if key not in seen:
                unique.append(row)
                seen.add(key)
        selected = unique
    unique_count = len(selected)
    if 'count_by' in options:
        counts = Counter(json.dumps(pointer(row, options['count_by']), sort_keys=True, ensure_ascii=False) for row in selected)
        selected = [{'value': json.loads(key), 'count': count} for key, count in counts.items()]
    offset, limit = options.get('offset', 0), options.get('limit', 50)
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 0 <= limit <= 1000:
        raise ValueError('分页范围无效')
    page = selected[offset:offset+limit]
    if 'fields' in options:
        fields = options['fields']
        if not isinstance(fields, list) or len(fields) > 32 or any(not isinstance(field, str) for field in fields):
            raise ValueError('投影字段无效')
        page = [{field: pointer(row, field) for field in fields} for row in page]
    return {'rows': page, 'coverage': {'input': total, 'expanded': expanded_count, 'matched': matched, 'unique': unique_count,
        'offset': offset, 'returned': len(page), 'available': len(selected),
        'complete': offset == 0 and len(page) == len(selected)}}
